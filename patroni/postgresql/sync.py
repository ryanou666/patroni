import logging
import re
import time

from copy import deepcopy
from typing import Collection, List, NamedTuple, Tuple, TYPE_CHECKING

from ..collections import CaseInsensitiveDict, CaseInsensitiveSet
from ..dcs import Cluster
from ..psycopg import quote_ident as _quote_ident
if TYPE_CHECKING:  # pragma: no cover
    from . import Postgresql

logger = logging.getLogger(__name__)

SYNC_STANDBY_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z_0-9\$]*$')
SYNC_REP_PARSER_RE = re.compile(r"""
           (?P<first> [fF][iI][rR][sS][tT] )    # 忽略大小写匹配fist
         | (?P<any> [aA][nN][yY] )      # 忽略大小写匹配any
         | (?P<space> \s+ )     # 空白字符
         | (?P<ident> [A-Za-z_][A-Za-z_0-9\$]* )    # 以大小写字母或下划线开头，后面跟着大小写字母、下划线、数字、$ 的标识符
         | (?P<dquot> " (?: [^"]+ | "" )* " )   # 匹配双引号内的字符串，包含的双引号用双引号转义
         | (?P<star> [*] )      # 匹配*
         | (?P<num> \d+ )       # 匹配数字
         | (?P<comma> , )       # 匹配逗号
         | (?P<parenstart> \( ) # 匹配左圆括号
         | (?P<parenend> \) )   # 匹配右圆括号
         | (?P<JUNK> . )        # 匹配任意字符
        """, re.X)


def quote_ident(value: str) -> str:
    """Very simplified version of `psycopg` :func:`quote_ident` function."""
    return value if SYNC_STANDBY_NAME_RE.match(value) else _quote_ident(value)


class _SSN(NamedTuple):
    """class representing "synchronous_standby_names" value after parsing.

    :ivar sync_type: possible values: 'off', 'priority', 'quorum'
    :ivar has_star: is set to `True` if "synchronous_standby_names" contains '*'
    :ivar num: how many nodes are required to be synchronous
    :ivar members: collection of standby names listed in "synchronous_standby_names"
    """
    """"
    _SSN 类用于表示 PostgreSQL 数据库配置中的 synchronous_standby_names 参数解析后的值
    :ivar sync_type: 可能的值有 'off'、'priority' 或 'quorum'
    :ivar has_star: 如果 synchronous_standby_names 参数中包含 '*'，则此字段设置为 True。
    :ivar num: 一个整数，表示需要多少个节点同步。
    :ivar members: 存储在 synchronous_standby_names 参数中列出的备用节点名称，集合。
    """
    sync_type: str # 同步的类型(quorum 或者 priority)
    has_star: bool # 节点名中是否配置有*
    num: int    # 同步备个数
    members: CaseInsensitiveSet # 所有配置的同步备名集合（忽略大小写）


_EMPTY_SSN = _SSN('off', False, 0, CaseInsensitiveSet())


def parse_sync_standby_names(value: str) -> _SSN:
    """Parse postgresql synchronous_standby_names to constituent parts.
        将 postgresql synchronous_standby_names 解析为组成部分。
    
    :param value: the value of `synchronous_standby_names`
    :returns: :class:`_SSN` object
    :raises `ValueError`: if the configuration value can not be parsed

    >>> parse_sync_standby_names('').sync_type
    'off'

    >>> parse_sync_standby_names('FiRsT').sync_type
    'priority'

    >>> 'first' in parse_sync_standby_names('FiRsT').members
    True

    >>> set(parse_sync_standby_names('"1"').members)
    {'1'}

    >>> parse_sync_standby_names(' a , b ').members == {'a', 'b'}
    True

    >>> parse_sync_standby_names(' a , b ').num
    1

    >>> parse_sync_standby_names('ANY 4("a",*,b)').has_star
    True

    >>> parse_sync_standby_names('ANY 4("a",*,b)').num
    4

    >>> parse_sync_standby_names('1')  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    ValueError: Unparseable synchronous_standby_names value

    >>> parse_sync_standby_names('a,')  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    ValueError: Unparseable synchronous_standby_names value

    >>> parse_sync_standby_names('ANY 4("a" b,"c c")')  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    ValueError: Unparseable synchronous_standby_names value

    >>> parse_sync_standby_names('FIRST 4("a",)')  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    ValueError: Unparseable synchronous_standby_names value

    >>> parse_sync_standby_names('2 (,)')  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
        ...
    ValueError: Unparseable synchronous_standby_names value
    """
    # 返回三元组列表 (匹配的组名, 匹配的内容, token的开始位置)，空白会忽略
    tokens = [(m.lastgroup, m.group(0), m.start())
              for m in SYNC_REP_PARSER_RE.finditer(value)
              if m.lastgroup != 'space']
    if not tokens:
        return deepcopy(_EMPTY_SSN)

    if [t[0] for t in tokens[0:3]] == ['any', 'num', 'parenstart'] and tokens[-1][0] == 'parenend':
        # 前三个元组是 any 数字 左圆括号，最后一个元组是右圆括号
        sync_type = 'quorum' # 同步方式为 法定人数
        num = int(tokens[1][1]) # 法定人数的个数
        synclist = tokens[3:-1] # 法定人数的名字
    elif [t[0] for t in tokens[0:3]] == ['first', 'num', 'parenstart'] and tokens[-1][0] == 'parenend':
        # 前三个元组是 first 数字 左圆括号，最后一个元组是右圆括号
        sync_type = 'priority'  # 同步方式为优先级方式
        num = int(tokens[1][1]) # 同步个数
        synclist = tokens[3:-1] # 同步的名字
    elif [t[0] for t in tokens[0:2]] == ['num', 'parenstart'] and tokens[-1][0] == 'parenend':
        # 前两个元组是 数字 左圆括号，最后一个元组是右圆括号
        sync_type = 'priority'
        num = int(tokens[0][1])
        synclist = tokens[2:-1]
    else:
        # 直接是逗号分隔的同步备列表，就是优先级方式，且同步备个数是1，其他都是潜在同步备
        sync_type = 'priority'
        num = 1
        synclist = tokens

    has_star = False
    members = CaseInsensitiveSet()
    # enumerate 是 Python 中的一个内置函数，它用于将一个可迭代对象（如列表、元组、字符串等）组合为一个索引序列，同时列出数据和数据下标。
    for i, (a_type, a_value, a_pos) in enumerate(synclist):
        if i % 2 == 1:  # odd elements are supposed to be commas
            if len(synclist) == i + 1:  # except the last token
                raise ValueError("Unparseable synchronous_standby_names value %r: Unexpected token %s %r at %d" %
                                 (value, a_type, a_value, a_pos))
            elif a_type != 'comma':
                raise ValueError("Unparseable synchronous_standby_names value %r: ""Got token %s %r while"
                                 " expecting comma at %d" % (value, a_type, a_value, a_pos))
        elif a_type in {'ident', 'first', 'any'}:
            # 一个节点名字符合标识符规则或者是关键字fist or any
            members.add(a_value)
        elif a_type == 'star':
            # 节点名字中有*
            members.add(a_value)
            has_star = True
        elif a_type == 'dquot':
            # 匹配双引号里面保存的名字，双引号中间两个引号修改成一个 -- 外面这一层双引号直接去掉
            members.add(a_value[1:-1].replace('""', '"'))
        else:
            raise ValueError("Unparseable synchronous_standby_names value %r: Unexpected token %s %r at %d" %
                             (value, a_type, a_value, a_pos))
    # 返回的是 同步的类型(quorum 或者 priority) 节点名中是否配置有* 同步备个数 所有配置的同步备名集合
    return _SSN(sync_type, has_star, num, members)


class _Replica(NamedTuple):
    """Class representing a single replica that is eligible to be synchronous.

    Attributes are taken from ``pg_stat_replication`` view and respective ``Cluster.members``.

    :ivar pid: PID of walsender process.
    :ivar application_name: matches with the ``Member.name``.
    :ivar sync_state: possible values are: ``async``, ``potential``, ``quorum``, and ``sync``.
    :ivar lsn: ``write_lsn``, ``flush_lsn``, or ``replay_lsn``, depending on the value of ``synchronous_commit`` GUC.
    :ivar nofailover: whether the corresponding member has ``nofailover`` tag set to ``True``.
    """
    pid: int
    application_name: str
    sync_state: str
    lsn: int
    nofailover: bool


class _ReplicaList(List[_Replica]):
    """A collection of :class:``_Replica`` objects.

    Values are reverse ordered by ``_Replica.sync_state`` and ``_Replica.lsn``.
    That is, first there will be replicas that have ``sync_state`` == ``sync``, even if they are not
    the most up-to-date in term of write/flush/replay LSN. It helps to keep the result of chosing new
    synchronous nodes consistent in case if a synchronous standby member is slowed down OR async node
    is receiving changes faster than the sync member. Such cases would trigger sync standby member
    swapping, but only if lag on this member is exceeding a threshold (``maximum_lag_on_syncnode``).

    :ivar max_lsn: maximum value of ``_Replica.lsn`` among all values. In case if there is just one
                   element in the list we take value of ``pg_current_wal_lsn()``.
    """

    def __init__(self, postgresql: 'Postgresql', cluster: Cluster) -> None:
        """Create :class:``_ReplicaList`` object.

        :param postgresql: reference to :class:``Postgresql`` object.
        :param cluster: currently known cluster state from DCS.
        """
        super().__init__()

        # We want to prioritize candidates based on `write_lsn``, ``flush_lsn``, or ``replay_lsn``.
        # Which column exactly to pick depends on the values of ``synchronous_commit`` GUC.
        # 我们希望根据 'write_lsn'、 'flush_lsn' 或 'replay_lsn' 对候选者进行优先排序。具体选择哪一列取决于 'synchronous_commit' GUC 的值。
        # 当前有三种情况： 'replay_lsn' 'write_lsn' 'flush_lsn'
        sort_col = {
            'remote_apply': 'replay',
            'remote_write': 'write'
        }.get(postgresql.synchronous_commit(), 'flush') + '_lsn'

        # 这个是 patroni 启动时候执行的yml文件中的name字段
        members = CaseInsensitiveDict({m.name: m for m in cluster.members})
        for row in postgresql.pg_stat_replication():
            # 我们可以在这里看到，application_name需要和yml配置文件中的节点名一样！！！
            member = members.get(row['application_name'])

            # We want to consider only rows from ``pg_stat_replication` that:
            # 我们仅考虑 pg_stat_replication 中满足以下条件的行：
            # 1. are known to be streaming (write/flush/replay LSN are not NULL).
            #   这些备库的 write/flush/replay LSN 不能为空
            # 2. can be mapped to a ``Member`` of the ``Cluster``:
            #   a. ``Member`` doesn't have ``nosync`` tag set;
            #       这个成员没有 nosync 标签
            #   b. PostgreSQL on the member is known to be running and accepting client connections.
            #       这个成员数据库需要正在运行且可以接收客户端连接
            if member and row[sort_col] is not None and member.is_running and not member.tags.get('nosync', False):
                self.append(_Replica(row['pid'], row['application_name'],
                                     row['sync_state'], row[sort_col], bool(member.nofailover)))

        # Prefer replicas that are in state ``sync`` and with higher values of ``write``/``flush``/``replay`` LSN.
        # 将会优先选取备库：首先是 sync 状态的备库，其次是 lsn更新的备库
        self.sort(key=lambda r: (r.sync_state, r.lsn), reverse=True)

        self.max_lsn = max(self, key=lambda x: x.lsn).lsn if len(self) > 1 else postgresql.last_operation()


class SyncHandler(object):
    """Class responsible for working with the `synchronous_standby_names`.

    Sync standbys are chosen based on their state in `pg_stat_replication`.
    When `synchronous_standby_names` is changed we memorize the `_primary_flush_lsn`
    and the `current_state()` method will count newly added names as "sync" only when
    they reached memorized LSN and also reported as "sync" by `pg_stat_replication`"""

    """负责管理参数 `synchronous_standby_names` 的类。

    根据 `pg_stat_replication` 中的状态选择同步备。
    当 `synchronous_standby_names` 发生更改时，我们会记住 `_primary_flush_lsn`
    并且 `current_state()` 方法仅在新添加的节点名称 达到这个记住的LSN 并且 这个节点由 `pg_stat_replication` 报告为"sync"时才将其计为"sync" 
    """

    def __init__(self, postgresql: 'Postgresql') -> None:
        self._postgresql = postgresql
        self._synchronous_standby_names = ''  # last known value of synchronous_standby_names 最新最近知道的synchronous_standby_names参数内容默认''
        self._ssn_data = deepcopy(_EMPTY_SSN)
        self._primary_flush_lsn = 0
        # "sync" replication connections, that were verified to reach self._primary_flush_lsn at some point
        self._ready_replicas = CaseInsensitiveDict({})  # keys: member names, values: connection pids

    # 此函数实际上只有主库或者主节点才会调用到
    # 缓存新的 synchronous_standby_names 信息
    def _handle_synchronous_standby_names_change(self) -> None:
        """Handles changes of "synchronous_standby_names" GUC.

        If "synchronous_standby_names" was changed, we need to check that newly added replicas have
        reached `self._primary_flush_lsn`. Only after that they could be counted as synchronous.
        """

        """处理 "synchronous_standby_names" GUC 的更改。

        如果 "synchronous_standby_names" 已更改，我们需要检查新添加的副本是否已
        达到 `self._primary_flush_lsn`。只有在此之后，它们才能算作同步。
        """

        # 执行sql（或者从上一次执行结果中）获取配置信息
        synchronous_standby_names = self._postgresql.synchronous_standby_names()
        if synchronous_standby_names == self._synchronous_standby_names:
            # 参数没有改变直接返回
            return

        # 参数改变了，缓存下来，用于下次判断参数改变的依据
        self._synchronous_standby_names = synchronous_standby_names
        try:
            # 同步的类型(quorum 或者 priority) 节点名中是否配置有* 同步备个数 所有配置的同步备名集合
            self._ssn_data = parse_sync_standby_names(synchronous_standby_names)
        except ValueError as e:
            logger.warning('%s', e)
            # 解析错了先当于这个参数什么都不配置
            self._ssn_data = deepcopy(_EMPTY_SSN)

        # Invalidate cache of "sync" connections
        for app_name in list(self._ready_replicas.keys()):
            if app_name not in self._ssn_data.members:
                # 如果当前缓存的sync连接的节点不在新获取的同步节点中，则直接删除这个节点
                del self._ready_replicas[app_name]

        # Newly connected replicas will be counted as sync only when reached self._primary_flush_lsn
        # 新连接的副本只有在达到 self._primary_flush_lsn 时才会被视为同步
        # 当前是主库返回的是写入lsn位置，否则返回的是接收（考虑的是备）或者回放(应该考虑的是主降备后？)的最大位置
        #   能调用进来的都是主库或者主节点，因此这里不需要考虑备库的方式
        self._primary_flush_lsn = self._postgresql.last_operation()
        # Ensure some WAL traffic to move replication
        self._postgresql.query("""DO $$
BEGIN
    SET local synchronous_commit = 'off';
    PERFORM * FROM pg_catalog.txid_current();
END;$$""")
        # 重置状态，以便下次获取集群信息能够重新往主库执行sql获取
        self._postgresql.reset_cluster_info_state(None)  # Reset internal cache to query fresh values

    def _process_replica_readiness(self, cluster: Cluster, replica_list: _ReplicaList) -> None:
        """Flags replicas as truly "synchronous" when they have caught up with ``_primary_flush_lsn``.

        :param cluster: current cluster topology from DCS
        :param replica_list: collection of replicas that we want to evaluate.
        """
        for replica in replica_list:
            # if standby name is listed in the /sync key we can count it as synchronous, otherwise
            # it becomes really synchronous when sync_state = 'sync' and it is known that it managed to catch up
            if replica.application_name not in self._ready_replicas\
                    and replica.application_name in self._ssn_data.members\
                    and (cluster.sync.matches(replica.application_name)
                         or replica.sync_state == 'sync' and replica.lsn >= self._primary_flush_lsn):
                self._ready_replicas[replica.application_name] = replica.pid

    # 只有主库才会调用，用于挑选最优的候选节点（用于同步备）
    def current_state(self, cluster: Cluster) -> Tuple[CaseInsensitiveSet, CaseInsensitiveSet]:
        """Find the best candidates to be the synchronous standbys.

        Current synchronous standby is always preferred, unless it has disconnected or does not want to be a
        synchronous standby any longer.

        Standbys are selected based on values from the global configuration:

        - `maximum_lag_on_syncnode`: would help swapping unhealthy sync replica in case if it stops
          responding (or hung). Please set the value high enough so it won't unncessarily swap sync
          standbys during high loads. Any value less or equal of 0 keeps the behavior backward compatible.
          Please note that it will not also swap sync standbys in case where all replicas are hung.
        - `synchronous_node_count`: controlls how many nodes should be set as synchronous.

        :returns: tuple of candidates :class:`CaseInsensitiveSet` and synchronous standbys :class:`CaseInsensitiveSet`.
        """

        """找到最佳候选者作为同步备用节点。

        当前同步备用节点始终是首选，除非它已断开连接或不想再作为同步备用节点。

        根据全局配置中的值选择备用节点（这里应该指的是同步备）：
        - `maximum_lag_on_syncnode`：如果同步副本停止响应（或挂起），则有助于交换不健康的同步副本。
            请将值设置得足够高，以免在高负载期间不必要地交换同步备用节点。任何小于或等于 0 的值都会使行为保持向后兼容。
            请注意，如果所有副本都挂起，它也不会交换同步备用节点。
        - `synchronous_node_count`：控制应将多少个节点设置为同步节点。

        :returns: 候选节点 :class:`CaseInsensitiveSet` 和同步备用节点 :class:`CaseInsensitiveSet` 的元组。
        """

        # 缓存新的 synchronous_standby_names 信息
        self._handle_synchronous_standby_names_change()

        replica_list = _ReplicaList(self._postgresql, cluster)
        self._process_replica_readiness(cluster, replica_list)

        if TYPE_CHECKING:  # pragma: no cover
            assert self._postgresql.global_config is not None
        sync_node_count = self._postgresql.global_config.synchronous_node_count\
            if self._postgresql.supports_multiple_sync else 1
        sync_node_maxlag = self._postgresql.global_config.maximum_lag_on_syncnode

        candidates = CaseInsensitiveSet()
        sync_nodes = CaseInsensitiveSet()
        # Prefer members without nofailover tag. We are relying on the fact that sorts are guaranteed to be stable.
        for replica in sorted(replica_list, key=lambda x: x.nofailover):
            if sync_node_maxlag <= 0 or replica_list.max_lsn - replica.lsn <= sync_node_maxlag:
                candidates.add(replica.application_name)
                if replica.sync_state == 'sync' and replica.application_name in self._ready_replicas:
                    sync_nodes.add(replica.application_name)
            if len(candidates) >= sync_node_count:
                break

        return candidates, sync_nodes

    # 只有当前是主节点才会执行
    def set_synchronous_standby_names(self, sync: Collection[str]) -> None:
        """Constructs and sets "synchronous_standby_names" GUC value.

        :param sync: set of nodes to sync to
        """
        has_asterisk = '*' in sync
        if has_asterisk:
            sync = ['*']
        else:
            sync = [quote_ident(x) for x in sync]

        if self._postgresql.supports_multiple_sync and len(sync) > 1:
            sync_param = '{0} ({1})'.format(len(sync), ','.join(sync))
        else:
            sync_param = next(iter(sync), None)

        if not (self._postgresql.config.set_synchronous_standby_names(sync_param)
                and self._postgresql.state == 'running' and self._postgresql.is_leader()) or has_asterisk:
            return

        time.sleep(0.1)  # Usualy it takes 1ms to reload postgresql.conf, but we will give it 100ms

        # Reset internal cache to query fresh values
        self._postgresql.reset_cluster_info_state(None)

        # timeline == 0 -- indicates that this is the replica
        if self._postgresql.get_primary_timeline() > 0:
            self._handle_synchronous_standby_names_change()
