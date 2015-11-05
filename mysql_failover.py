#!/usr/bin/env python
import argparse
import os
import time
import uuid

import MySQLdb

import launch_replacement_db_host
import modify_mysql_zk
from lib import mysql_lib
from lib import host_utils
from lib import environment_specific

MAX_ALIVE_MASTER_SLAVE_LAG_SECONDS = 60
MAX_DEAD_MASTER_SLAVE_LAG_SECONDS = 3600
MAX_ZK_WRITE_ATTEMPTS = 5
WAIT_TIME_CONFIRM_QUIESCE = 10


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('instance',
                        help='The master to be demoted')
    parser.add_argument('--trust_me_its_dead',
                        help=('You say you know what you are doing. We are '
                              'going to trust you and hope for the best'),
                        default=False,
                        action='store_true')
    parser.add_argument('--ignore_dr_slave',
                        help=('Need to promote, but alreaedy have a dead '
                              'dr_slave? This option is what you looking '
                              'for. The dr_slave will be completely '
                              'ignored.'),
                        default=False,
                        action='store_true')
    parser.add_argument('--dry_run',
                        help=('Do not actually run a promotion, just run '
                              'safety checks, etc...'),
                        default=False,
                        action='store_true')
    parser.add_argument('--skip_lock',
                        help=('Do not take a promotion lock. Scary.'),
                        default=False,
                        action='store_true')
    parser.add_argument('--kill_old_master',
                        help=('If we can not get the master into read_only, '
                              ' send a mysqladmin kill to the old master.'),
                        default=False,
                        action='store_true')
    args = parser.parse_args()

    instance = host_utils.HostAddr(args.instance)
    mysql_failover(instance, args.dry_run, args.skip_lock,
                   args.ignore_dr_slave, args.trust_me_its_dead, args.kill_old_master)


def mysql_failover(master, dry_run, skip_lock,
                   ignore_dr_slave, trust_me_its_dead, kill_old_master):
    """ Promte a new MySQL master

    Args:
    master - Hostaddr object of the master instance to be demoted
    dry_run - Do not change state, just do sanity testing and exit
    skip_lock - Do not take a promotion lock
    ignore_dr_slave - Ignore the existance of a dr_slave
    trust_me_its_dead - Do not test to see if the master is dead
    kill_old_master - Send a mysqladmin kill command to the old master

    Returns:
    new_master - The new master server
    """
    log.info('Master to demote is {master}'.format(master=master))

    zk = host_utils.MysqlZookeeper()
    (replica_set, _) = zk.get_replica_set_from_instance(master, rtypes=['master'])
    log.info('Replica set is detected as '
             '{replica_set}'.format(replica_set=replica_set))

    # take a lock here to make sure nothing changes underneath us
    if not skip_lock and not dry_run:
        log.info('Taking promotion lock on replica set')
        lock_identifier = get_promotion_lock(replica_set)
    else:
        lock_identifier = None

    # giant try. If there any problems we roll back from the except
    try:
        master_conn = False
        slave = zk.get_mysql_instance_from_replica_set(replica_set=replica_set,
                                                       repl_type=host_utils.REPLICA_ROLE_SLAVE)
        log.info('Slave/new master is detected as {slave}'.format(slave=slave))

        if ignore_dr_slave:
            log.info('Intentionally ignoring a dr_slave')
            dr_slave = None
        else:
            dr_slave = zk.get_mysql_instance_from_replica_set(replica_set,
                                                              host_utils.REPLICA_ROLE_DR_SLAVE)
        log.info('DR slave is detected as {dr_slave}'.format(dr_slave=dr_slave))
        if dr_slave:
            if dr_slave == slave:
                raise Exception('Slave and dr_slave appear to be the same')

            replicas = set([slave, dr_slave])
        else:
            replicas = set([slave])

        # let's make sure that what we think is the master, actually is
        confirm_replica_topology(master, replicas)

        # We use master_conn as a mysql connection to the master server, if
        # it is False, the master is dead
        if trust_me_its_dead:
            master_conn = None
        else:
            master_conn = is_master_alive(master, replicas)
        slave_conn = mysql_lib.connect_mysql(slave)

        # Test to see if the slave is setup for replication. If not, we are hosed
        log.info('Testing to see if Slave/new master is setup to write '
                 'replication logs')
        try:
            mysql_lib.get_master_status(slave_conn)
        except mysql_lib.ReplicationError:
            log.error('New master {slave} is not setup to write replicaiton '
                      'logs!'.format(slave=slave))
            raise
        log.info('Slave/new master is setup to write replication logs')

        if kill_old_master:
            log.info('Killing old master, we hope you know what you are doing')
            mysql_lib.shutdown_mysql(master)
            master_conn = None

        if master_conn:
            log.info('Master is considered alive')
            dead_master = False
            confirm_max_replica_lag(replicas, MAX_ALIVE_MASTER_SLAVE_LAG_SECONDS,
                                    dead_master=dead_master)
        else:
            log.info('Master is considered dead')
            dead_master = True
            confirm_max_replica_lag(replicas, MAX_DEAD_MASTER_SLAVE_LAG_SECONDS,
                                    dead_master=dead_master)

        if dry_run:
            log.info('In dry_run mode, so exiting now')
            # Using os._exit in order to not get catch in the giant try
            os._exit(0)

        log.info('Preliminary sanity checks complete, starting promotion')

        if master_conn:
            log.info('Setting read_only on master')
            mysql_lib.set_global_variable(master_conn, 'read_only', True)
            log.info('Confirming no writes to old master')
            # If there are writes with the master in read_only mode then the
            # promotion can not proceed.
            # A likely reason is a client has the SUPER privilege.
            confirm_no_writes(master_conn)
            log.info('Waiting for replicas to be caught up')
            confirm_max_replica_lag(replicas, 0,
                                    timeout=MAX_ALIVE_MASTER_SLAVE_LAG_SECONDS,
                                    dead_master=dead_master)
            log.info('Setting up replication from old master ({master})'
                     'to new master ({slave})'.format(master=master,
                                                      slave=slave))
            mysql_lib.setup_replication(new_master=slave, new_replica=master)
        else:
            log.info('Starting up a zk connection to make sure we can connect')
            kazoo_client = environment_specific.get_kazoo_client()
            if not kazoo_client:
                raise Exception('Could not conect to zk')

            log.info('Confirming replica has processed all replication '
                     ' logs')
            confirm_no_writes(slave_conn)
            log.info('Looks like no writes being processed by replica via '
                     'replication or other means')
            if len(replicas) > 1:
                log.info('Confirming relpica servers in sync')
                confirm_max_replica_lag(replicas, MAX_DEAD_MASTER_SLAVE_LAG_SECONDS,
                                        replicas_synced=True,
                                        dead_master=dead_master)
    except:
        log.info('Starting rollback')
        if master_conn:
            log.info('Releasing read_only on old master')
            mysql_lib.set_global_variable(master_conn, 'read_only', False)

            log.info('Clearing replication settings on old master')
            mysql_lib.reset_slave(master_conn)
        if lock_identifier:
            log.info('Releasing promotion lock')
            release_promotion_lock(lock_identifier)
        log.info('Rollback complete, reraising exception')
        raise

    if dr_slave:
        try:
            mysql_lib.setup_replication(new_master=slave, new_replica=dr_slave)
        except Exception as e:
            log.error(e)
            log.error('Setting up replication on the dr_slave failed. '
                      'Failing forward!')

    log.info('Updating zk')
    zk_write_attempt = 0
    while True:
        try:
            modify_mysql_zk.swap_master_and_slave(slave, dry_run=False)
            break
        except:
            if zk_write_attempt > MAX_ZK_WRITE_ATTEMPTS:
                log.info('Final failure writing to zk, bailing')
                raise
            else:
                log.info('Write to zk failed, trying again')
                zk_write_attempt = zk_write_attempt+1

    log.info('Removing read_only from new master')
    mysql_lib.set_global_variable(slave_conn, 'read_only', False)
    log.info('Removing replication configuration from new master')
    mysql_lib.reset_slave(slave_conn)
    if lock_identifier:
        log.info('Releasing promotion lock')
        release_promotion_lock(lock_identifier)

    log.info('Failover complete')

    if not master_conn:
        log.info('As master is dead, will try to launch a replacement. Will '
                 'sleep 20 seconds first to let things settle')
        time.sleep(20)
        launch_replacement_db_host.launch_replacement_db_host(master)


def get_promotion_lock(replica_set):
    """ Take a promotion lock

    Args:
    replica_set - The replica set to take the lock against

    Returns:
    A unique identifer for the lock
    """
    lock_identifier = str(uuid.uuid4())
    log.info('Promotion lock identifier is '
             '{lock_identifier}'.format(lock_identifier=lock_identifier))

    conn = mysql_lib.get_mysqlops_connections()

    log.info('Releasing any expired locks')
    release_expired_promotion_locks(conn)

    log.info('Checking existing locks')
    check_promotion_lock(conn, replica_set)

    log.info('Taking lock against replica set: '
             '{replica_set}'.format(replica_set=replica_set))
    params = {'lock': lock_identifier,
              'localhost': host_utils.HOSTNAME,
              'replica_set': replica_set,
              'user': host_utils.get_user()}
    sql = ("INSERT INTO mysqlops.promotion_locks "
           "SET "
           "lock_identifier = %(lock)s, "
           "lock_active = 'active', "
           "created_at = NOW(), "
           "expires = NOW() + INTERVAL 12 HOUR, "
           "released = NULL, "
           "replica_set = %(replica_set)s, "
           "promoting_host = %(localhost)s, "
           "promoting_user = %(user)s ")
    cursor = conn.cursor()
    cursor.execute(sql, params)
    conn.commit()
    log.info(cursor._executed)
    return lock_identifier


def release_expired_promotion_locks(lock_conn):
    """ Release any locks which have expired

    Args:
    lock_conn - a mysql connection to the mysql instance storing locks
    """
    cursor = lock_conn.cursor()
    # There is a unique index on (replica_set,lock_active), so a replica set
    # may not have more than a single active promotion in flight. We therefore
    # can not set lock_active = 'inactive' as only a single entry would be
    # allowed for inactive.
    sql = ('UPDATE mysqlops.promotion_locks '
           'SET lock_active = NULL '
           'WHERE expires < now()')
    cursor.execute(sql)
    lock_conn.commit()
    log.info(cursor._executed)


def check_promotion_lock(lock_conn, replica_set):
    """ Confirm there are no active locks that would block taking a
        promotion lock

    Args:
    lock_conn - a mysql connection to the mysql instance storing locks
    replica_set - the replica set that should be locked
    """
    cursor = lock_conn.cursor()
    params = {'replica_set': replica_set}
    sql = ('SELECT lock_identifier, promoting_host, promoting_user '
           'FROM mysqlops.promotion_locks '
           "WHERE lock_active = 'active' AND "
           "replica_set = %(replica_set)s")
    cursor.execute(sql, params)
    ret = cursor.fetchone()
    if ret is not None:
        log.error('Lock is already held by {lock}'.format(lock=ret))
        log.error(('To relase this lock you can connect to the mysqlops '
                   'db by running: '))
        log.error('/usr/local/bin/mysql_utils/mysql_cli.py mysqlopsdb001 '
                  '-p read-write ')
        log.error('And then running the following query:')
        log.error(('UPDATE mysqlops.promotion_locks '
                   'SET lock_active = NULL AND released = NOW() '
                   'WHERE lock_identifier = '
                  "'{lock}';".format(lock=ret['lock_identifier'])))
        raise Exception('Can not take promotion lock')


def release_promotion_lock(lock_identifier):
    """ Release a promotion lock

    Args:
    lock_identifier - The lock to release
    """
    conn = mysql_lib.get_mysqlops_connections()
    cursor = conn.cursor()

    params = {'lock_identifier': lock_identifier}
    sql = ('UPDATE mysqlops.promotion_locks '
           'SET lock_active = NULL AND released = NOW() '
           'WHERE lock_identifier = %(lock_identifier)s')
    cursor.execute(sql, params)
    conn.commit()
    log.info(cursor._executed)


def confirm_replica_topology(master, replicas):
    """ Confirm that replica servers are actually replicating off of a master

    Args:
    master - A hostaddr object for the master instance
    replicas - A set of hostaddr objects for the replica instance
    """
    for replica in replicas:
        conn = mysql_lib.connect_mysql(replica)
        ss = mysql_lib.get_slave_status(conn)
        repl_master = host_utils.HostAddr(':'.join((ss['Master_Host'],
                                                    str(ss['Master_Port']))))
        if repl_master != master:
            raise Exception('Slave {replica} is not a replica of master '
                            '{master}, but is instead a replica of '
                            '{repl_master}'.format(replica=replica,
                                                   repl_master=repl_master,
                                                   master=master))
        else:
            log.info('Replica {replica} is replicating from expected master '
                     'server {master}'.format(replica=replica,
                                              master=master))


def confirm_max_replica_lag(replicas, max_lag, dead_master,
                            replicas_synced=False, timeout=0):
    """ Test replication lag

    Args:
    replicas - A set of hostaddr object to be tested for replication lag
    max_lag - Max computed replication lag in seconds. If 0 is supplied,
              then exec position is compared from replica servers to the
              master rather than using a computed second behind as the
              heartbeat will be blocked by read_only.
    replicas_synced - Replica servers must have executed to the same
                      position in the binary log.
    timeout - How long to wait for replication to be in the desired state
    """
    repl_checks = dict()
    start = time.time()
    while True:
        acceptable = True
        for replica in replicas:
            repl_check = mysql_lib.calc_slave_lag(replica, dead_master=dead_master)
            repl_checks[replica.__str__()] = ':'.join((repl_check['ss']['Relay_Master_Log_File'],
                                                       str(repl_check['ss']['Exec_Master_Log_Pos'])))
            # Basic sanity
            if repl_check['sbm'] is None:
                raise Exception('Computed replication is unavailible for {replica}, '
                                'perhaps restart pt-heartbeat '
                                'on the master?'.format(replica=replica))

            if repl_check['ss']['Slave_SQL_Running'] != 'Yes':
                raise Exception('SQL thread on replica {replica} is not running. '
                                'Perhaps run start slave?'.format(replica=replica))

            if max_lag == 0:
                if repl_check['sql_bytes'] != 0:
                    acceptable = False
                    log.warn('Unprocessed log on {replica} is {sql_bytes} '
                             'bytes  > 0'
                             ''.format(replica=replica,
                                       sql_bytes=repl_check['sql_bytes']))
                else:
                    log.info('{replica} is in sync with the '
                             'master'.format(replica=replica))
            else:
                if repl_check['sbm'] > max_lag:
                    acceptable = False
                    log.warn('Lag on {replica} is {lag} seconds is greater '
                             'than limit of '
                             '{limit}'.format(replica=replica,
                                              limit=max_lag,
                                              lag=repl_check['sbm']))
                else:
                    log.info('Lag on {replica} is {lag} is <= limit of '
                             '{limit}'.format(replica=replica,
                                              limit=max_lag,
                                              lag=repl_check['sbm']))

        if replicas_synced and len(set(repl_checks.values())) != 1:
            acceptable = False
            raise Exception('Replica servers are not in sync and replicas_synced '
                            'is set. Replication status: '
                            '{repl_checks}'.format(repl_checks=repl_checks))
        if acceptable:
            return
        elif (time.time() - start) > timeout:
            raise Exception('Replication is not in an acceptable state')
        else:
            log.info('Sleeping for 5 second to allow replication to catch up')
            time.sleep(5)


def is_master_alive(master, replicas):
    """ Determine if the master is alive

    The function will:
    1. Attempt to connect to the master via the mysql protcol. If successful
       the master is considered alive.
    2. If #1 fails, check the io thread of the replica instance(s). If the io
       thread is not running, the master will be considered dead. If step #1
       fails and step #2 succeeds, we are in a weird state and will throw an
       exception.

    Args:
    master - A hostaddr object for the master instance
    replicas -  A set of hostaddr objects for the replica instances

    Returns:
    A mysql connection to the master if the master is alive, False otherwise.
    """
    if len(replicas) == 0:
        raise Exception('At least one replica must be present to determine '
                        'a master is dead')
    try:
        master_conn = mysql_lib.connect_mysql(master)
        return master_conn
    except MySQLdb.OperationalError as detail:
        (error_code, msg) = detail.args
        if error_code != mysql_lib.MYSQL_ERROR_CONN_HOST_ERROR:
            raise
        master_conn = False
        log.info('Unable to connect to current master {master} from '
                 '{hostname}, will check replica servers beforce declaring '
                 'the master dead'.format(master=master,
                                          hostname=host_utils.HOSTNAME))
    except:
        log.info('This is an unknown connection error. If you are very sure '
                 'that the master is dead, please put a "return False" at the '
                 'top of is_master_alive and then send rwultsch a stack trace')
        raise

    # We can not get a connection to the master, so poll the replica servers
    for replica in replicas:
        conn = mysql_lib.connect_mysql(replica)
        # If replication has not hit a timeout, a dead master can still have
        # a replica which thinks it is ok. "STOP SLAVE; START SLAVE" followed
        # by a sleep will get us truthyness.
        mysql_lib.restart_replication(conn)
        ss = mysql_lib.get_slave_status(conn)
        if ss['Slave_IO_Running'] == 'Yes':
            raise Exception('Replica {replica} thinks it can connect to '
                            'master {master}, but failover script can not. '
                            'Possible network partition!'
                            ''.format(replica=replica,
                                      master=master))
        else:
            log.info('Replica {replica} also can not connect to master '
                     '{master}.'.format(replica=replica,
                                        master=master))
    return False


def confirm_no_writes(conn):
    """ Confirm that a server is not receiving any writes

    Args:
    conn - A mysql connection
    """
    mysql_lib.enable_and_flush_activity_statistics(conn)
    log.info('Waiting {length} seconds to confirm instance is no longer '
             'accepting writes'.format(length=WAIT_TIME_CONFIRM_QUIESCE))
    time.sleep(WAIT_TIME_CONFIRM_QUIESCE)
    db_activity = mysql_lib.get_dbs_activity(conn)

    active_db = set()
    for db in db_activity:
        if db_activity[db]['ROWS_CHANGED'] != 0:
            active_db.add(db)

    if active_db:
        raise Exception('DB {dbs} has been modified when it should have '
                        'no activity'.format(dbs=active_db))

    log.info('No writes after sleep, looks like we are good to go')

if __name__ == "__main__":
    log = environment_specific.setup_logging_defaults(__name__)
    main()