# coding: utf-8
import MySQLdb
# py2 mysql-python  py3 mysqlclient


class Field():
    pass


class Q():
    def __init__(self, *args, **kwargs):
        self.children = list(args) + list(kwargs.items())
        self.connector = 'AND'
        self.negated = False

    # 添加Q对象
    def add(self, data, conn):
        if not isinstance(data, Q):
            raise TypeError(data)
        if self.connector == conn:
            if not data.negated and (data.connector == conn or len(data) == 1):
                self.children.extend(data.children)
            else:
                self.children.append(data)
        else:
            obj = Q()
            obj.connector = conn
            obj.children = self.children[:]
            self.children = [obj, data]

    def _combine(self, other, conn):
        if not isinstance(other, Q):
            raise TypeError(other)
        obj = Q()
        obj.connector = conn
        obj.add(self, conn)
        obj.add(other, conn)
        return obj

    # 重载 |
    def __or__(self, other):
        return self._combine(other, 'OR')

    # 重载 &
    def __and__(self, other):
        return self._combine(other, 'AND')

    # 重载 ~
    def __invert__(self):
        obj = Q()
        obj.add(self, 'AND')
        obj.negated = not self.negated
        return obj

    # 构建sql查询语句
    def _sql_expr(self):
        sql_list = []
        params = []
        for child in self.children:
            if not isinstance(child, Q):
                temp_sql, temp_params = self.magic_query(child)
                sql_list.append(temp_sql)
                params.extend(temp_params)
            else:
                temp_sql, temp_params = child._sql_expr()
                if temp_sql and temp_params:
                    raw_sql = child.connector.join(temp_sql)
                    if child.negated:
                        raw_sql = ' not ( ' + raw_sql + ' ) '
                    elif child.connector != self.connector:
                        raw_sql = ' ( ' + raw_sql + ' ) '
                    sql_list.append(raw_sql)
                    params.extend(temp_params)
        return sql_list, params

    # 取得对应sql及参数
    def sql_expr(self):
        sql_list, params = self._sql_expr()
        return self.connector.join(sql_list), params

    # 处理双下划线特殊查询
    def magic_query(self, child_query):
        correspond_dict = {
            '': ' = %s ',
            'gt': ' > %s ',
            'gte': ' >= %s ',
            'lt': ' < %s ',
            'lte': ' <= %s ',
            'contains': ' like %%%s%% ',
            'startswith': ' like %s%% ',
            'endswith': ' like %%%s ',
        }

        raw_sql = ''
        params = []
        query_str, value = child_query
        if '__' in query_str:
            field, magic = query_str.split('__')
        else:
            field = query_str
            magic = ''
        temp_sql = correspond_dict.get(magic)
        if temp_sql:
            raw_sql = ' ' + field + temp_sql
            params = [value]
        elif magic == 'isnull':
            if value:
                raw_sql = ' ' + field + ' is null '
            else:
                raw_sql = ' ' + field + ' is not null '
        elif magic == 'range':
            raw_sql = ' ' + field + ' between %s and %s '
            params = value
        elif magic == 'in':
            if isinstance(value, (QuerySet, ValuesQuerySet)):
                subquery = value.query.clone()
                # todo QuerySet id
                if len(subquery.select) != 1:
                    raise TypeError('Cannot use a multi-field %s as a filter value.'
                                    % value.__class__.__name__)
                sub_sql, sub_params = subquery.sql_expr()
                raw_sql = ' ' + field + ' in ( ' + sub_sql[:-1] + ' ) '
                params = sub_params
            else:
                raw_sql = ' ' + field + ' in %s '
                params = [tuple(value)]

        return raw_sql, params

    def __len__(self):
        return len(self.children)

    def __nonzero__(self):
        return bool(self.children)

    def __bool__(self):
        return bool(self.children)

    def __repr__(self):
        if self.negated:
            return '(NOT (%s: %s))' % (self.connector, ', '.join([str(c) for c
                                                                  in self.children]))
        return '(%s: %s)' % (self.connector, ', '.join([str(c) for c in
                                                        self.children]))


class Query():
    def __init__(self, model):
        self.model = model
        self.fields_list = list(self.model.fields.keys())

        self.flat = False
        self.filter_Q = Q()
        self.exclude_Q = Q()
        self.limit_dict = {}
        self.order_fields = []
        self.select = list(self.model.fields.keys())

    def __str__(self):
        sql, params = self.sql_expr()
        return sql % params

    # 根据当前筛选条件构建sql、params
    def sql_expr(self, method='select', update_dict=None):
        params = []
        where_expr = ''

        if self.filter_Q or self.exclude_Q:
            where_expr += ' where '

        if self.filter_Q:
            temp_sql, temp_params = self.filter_Q.sql_expr()
            where_expr += '(' + temp_sql + ')'
            params.extend(temp_params)

        if self.exclude_Q:
            temp_sql, temp_params = self.exclude_Q.sql_expr()
            if params:
                where_expr += ' and '
            where_expr += ' not (' + temp_sql + ')'
            params.extend(temp_params)

        if self.order_fields:
            where_expr += ' order by '
            order_list = []
            for field in self.order_fields:
                if field[0] == '-':
                    field_name = field[1:]
                    order_list.append(field_name + ' desc ')
                else:
                    order_list.append(field)
            where_expr += ' , '.join(order_list)

        if update_dict and self.limit_dict:
            # 不支持切片更新
            raise TypeError('Cannot update a query once a slice has been taken.')

        # limit
        limit = self.limit_dict.get('limit')
        if limit is not None:
            where_expr += ' limit %s '
            params.append(limit)
        offset = self.limit_dict.get('offset')
        if offset is not None:
            where_expr += ' offset %s '
            params.append(offset)

        # 构建不同操作的sql语句
        if method == 'count':
            sql = 'select count(*) from %s %s;' % (self.model.__db_table__, where_expr)
        elif method == 'update' and update_dict:
            _keys = []
            _params = []
            for key, val in update_dict.items():
                if key not in self.fields_list:
                    continue
                _keys.append(key)
                _params.append(val)
            params = _params + params
            sql = 'update %s set %s %s;' % (
                self.model.__db_table__, ', '.join([key + ' = %s' for key in _keys]), where_expr)
        elif method == 'delete':
            sql = 'delete from %s %s;' % (self.model.__db_table__, where_expr)
        else:
            sql = 'select %s from %s %s;' % (', '.join(self.select), self.model.__db_table__, where_expr)
        return sql, tuple(params)

    # clone
    def clone(self):
        obj = Query(self.model)
        obj.filter_Q = self.filter_Q
        obj.exclude_Q = self.exclude_Q
        obj.order_fields = self.order_fields[:]
        obj.limit_dict.update(self.limit_dict)
        obj.select = self.select[:]
        return obj


class QuerySet():
    def __init__(self, model, query=None):
        self.model = model
        self.select_result = None
        self.query = query or Query(model)
        self.fields_list = list(self.model.fields.keys())

    # all函数，返回一个新的QuerySet对象（无筛选条件）
    def all(self):
        return self._clone()

    # filter函数，返回一个新的QuerySet对象
    def filter(self, *args, **kwargs):
        return self._filter_or_exclude(False, *args, **kwargs)

    # exclude函数，返回一个新的QuerySet对象
    def exclude(self, *args, **kwargs):
        return self._filter_or_exclude(True, *args, **kwargs)

    # first
    def first(self):
        try:
            return self.get_index(0)
        except IndexError:
            return None

    # count
    def count(self):
        if self.select_result is not None:
            return len(self.select_result)

        # limit查询特殊处理
        limit = self.query.limit_dict.get('limit', 0)
        offset = self.query.limit_dict.get('offset', 0)
        if limit or offset:
            # 构建无limit_dict的query
            count_query = self._clone()
            count_query.query.limit_dict = None
            all_count = count_query.count()
            # 根据实际数量及偏移量计算count
            if offset > all_count:
                select_count = 0
            elif offset + limit > all_count:
                select_count = all_count - offset
            else:
                select_count = limit
        else:
            # 无数量限制，使用count查询
            sql, params = self.query.sql_expr(method='count')
            (select_count,) = Database.execute(self.model.__db_label__, sql, params).fetchone()
        return select_count

    # update
    def update(self, **kwargs):
        if kwargs:
            sql, params = self.query.sql_expr(method='update', update_dict=kwargs)
            Database.execute(self.model.__db_label__, sql, params)

    # order_by函数，返回一个新的QuerySet对象
    def order_by(self, *args):
        obj = self._clone()
        obj.query.order_fields = args
        return obj

    # create
    def create(self, **kwargs):
        obj = self.model(**kwargs)
        obj.save()
        return obj

    # exists
    def exists(self):
        return bool(self.count())

    # delete
    def delete(self):
        sql, params = self.query.sql_expr(method='delete')
        Database.execute(self.model.__db_label__, sql, params)

    # values
    def values(self, *args):
        # 字段检查
        err_fields = set(args) - set(self.fields_list)
        if err_fields:
            raise TypeError('Cannot resolve keyword %s into field.' % list(err_fields)[0])

        if not args:
            args = self.fields_list
        return self._clone(ValuesQuerySet, args)

    # values_list
    def values_list(self, *args, **kwargs):
        # 字段检查
        err_fields = set(args) - set(self.fields_list)
        if err_fields:
            raise TypeError('Cannot resolve keyword %s into field.' % list(err_fields)[0])

        flat = kwargs.pop('flat', False)
        # flat 只能返回一个字段列表
        if flat and len(args) > 1:
            raise TypeError('flat is not valid when values_list is called with more than one field.')

        # 没有传入指定字段，返回全部
        if not args:
            args = self.fields_list
        return self._clone(ValuesListQuerySet, args, flat)

    # sql查询基础函数
    def select(self):
        if self.select_result is None:
            sql, params = self.query.sql_expr()
            self.select_result = Database.execute(self.model.__db_label__, sql, params).fetchall()

    def base_index(self, index):
        if self.select_result is None:
            index_query = self[index:index + 1]
            index_query.select()
            index_value = index_query.select_result[0]
        else:
            index_value = self.select_result[index]
        return index_value

    # 索引值查询
    def get_index(self, index):
        index_value = self.base_index(index)
        return self.model(**dict(zip(self.fields_list, index_value)))

    def _clone(self, klass=None, select=None, flat=False):
        if klass is None:
            klass = self.__class__
        query = self.query.clone()
        if select:
            query.select = select[:]
        if flat:
            query.flat = flat
        obj = klass(model=self.model, query=query)
        return obj

    # 根据传入的筛选条件，返回新的QuerySet对象
    def _filter_or_exclude(self, negate, *args, **kwargs):
        clone = self._clone()
        temp_Q = Q()
        for arg in args:
            temp_Q.add(arg, 'AND')
        for k, v in kwargs.items():
            temp_Q.children.append((k, v))
        if temp_Q:
            if negate:
                clone.query.exclude_Q.add(temp_Q, 'AND')
            else:
                clone.query.filter_Q.add(temp_Q, 'AND')
        return clone

    # 自定义切片及索引取值
    def __getitem__(self, index):
        if isinstance(index, slice):
            obj = self._clone()
            # 根据当前偏移量计算新的偏移量
            start = index.start or 0
            stop = index.stop
            self_offset = obj.query.limit_dict.get('offset', 0)
            self_limit = obj.query.limit_dict.get('limit')

            limit = None
            sffset = self_offset + start
            if stop is not None:
                limit = stop - start

                if self_limit and sffset > self_offset + self_limit:
                    sffset = self_offset
                    limit = 0
                elif self_limit and sffset + limit > self_offset + self_limit:
                    limit = self_offset + self_limit - sffset

            obj.query.limit_dict['offset'] = sffset
            if limit:
                obj.query.limit_dict['limit'] = limit
            # 返回新的QuerySet对象
            return obj
        elif isinstance(index, int):
            if index < 0:
                raise TypeError('Negative indexing is not supported.')
            # 取得对应索引值
            return self.get_index(index)
        else:
            return None

    # 返回自定义迭代器
    def __iter__(self):
        self.select()
        for value in self.select_result:
            inst = self.model(**dict(zip(self.fields_list, value)))
            yield inst

    def __nonzero__(self):
        return bool(self.count())

    def __bool__(self):
        return bool(self.count())

    def __repr__(self):
        return '<QuerySet Obj>'


class ValuesQuerySet(QuerySet):

    def __iter__(self):
        self.select()
        for value in self.select_result:
            inst = {field: value[index] for index, field in enumerate(self.query.select)}
            yield inst

    def get_index(self, index):
        index_value = self.base_index(index)
        return {field: index_value[f_index] for f_index, field in enumerate(self.query.select)}

    def __repr__(self):
        return '<ValuesQuerySet Obj>'


class ValuesListQuerySet(QuerySet):
    def __init__(self, *args, **kwargs):
        super(ValuesListQuerySet, self).__init__(*args, **kwargs)
        self.flat = self.query.flat
        self.select_field = self.query.select
        if self.flat and len(self.select_field) != 1:
            raise TypeError('flat is not valid when values_list is called with more than one field.')

    def __iter__(self):
        self.select()
        for value in self.select_result:
            if self.flat:
                yield value[0]
            else:
                yield value

    def get_index(self, index):
        index_value = self.base_index(index)
        if self.flat:
            return index_value[0]
        else:
            return index_value

    def __repr__(self):
        return '<ValuesListQuerySet Obj>'


class Manager():
    def __init__(self, model):
        self.model = model

    def get_queryset(self):
        return QuerySet(self.model)

    def all(self):
        return self.get_queryset()

    def count(self):
        return self.get_queryset().count()

    def filter(self, *args, **kwargs):
        return self.get_queryset().filter(*args, **kwargs)

    def exclude(self, *args, **kwargs):
        return self.get_queryset().exclude(*args, **kwargs)

    def first(self):
        return self.get_queryset().first()

    def exists(self):
        return self.get_queryset().exists()

    def create(self, **kwargs):
        return self.get_queryset().create(**kwargs)

    def order_by(self, *args):
        return self.get_queryset().order_by(*args)

    def values(self, *args):
        return self.get_queryset().values(*args)

    def values_list(self, *args):
        return self.get_queryset().values_list(*args)


class MetaModel(type):
    __db_table__ = None
    __db_label__ = None
    fields = {}

    def __init__(cls, name, bases, attrs):
        super(MetaModel, cls).__init__(name, bases, attrs)
        fields = {}
        for key, val in cls.__dict__.items():
            if isinstance(val, Field):
                fields[key] = val
                setattr(cls, key, None)
        cls.fields = fields
        cls.attrs = attrs
        cls.objects = Manager(cls)


def with_metaclass(meta, *bases):
    # 兼容2和3的元类  见 py2 future.utils.with_metaclass
    class metaclass(meta):
        __call__ = type.__call__
        __init__ = type.__init__

        def __new__(cls, name, this_bases, d):
            if this_bases is None:
                return type.__new__(cls, name, (), d)
            return meta(name, bases, d)

    return metaclass('temporary_class', None, {})


class Model(with_metaclass(MetaModel, dict)):

    def __init__(self, **kw):
        for k, v in kw.items():
            if k in self.fields:
                setattr(self, k, v)
            else:
                raise TypeError("'%s' is an invalid keyword argument for this function" % k)

    def __repr__(self):
        return '<%s obj>' % self.__class__.__name__

    def __nonzero__(self):
        return bool(self.__dict__)

    def __bool__(self):
        return bool(self.__dict__)

    def __eq__(self, obj):
        return self.__class__ == obj.__class__ and self.__dict__ == obj.__dict__

    def __hash__(self):
        kv_list = sorted(self.__dict__.items(), key=lambda x: x[0])
        return hash(','.join(['"%s":"%s"' % x for x in kv_list]) + str(self.__class__))

    def save(self):
        insert = 'insert ignore into %s(%s) values (%s);' % (
            self.__db_table__, ', '.join(self.__dict__.keys()), ', '.join(['%s'] * len(self.__dict__)))
        return Database.execute(self.__db_label__, insert, self.__dict__.values())


# 数据库调用
class Database():
    autocommit = True
    conn = {}
    db_config = {}

    @classmethod
    def connect(cls, **databases):
        for db_label, db_config in databases.items():
            cls.conn[db_label] = MySQLdb.connect(host=db_config.get('host', 'localhost'),
                                                 port=int(db_config.get('port', 3306)),
                                                 user=db_config.get('user', 'root'),
                                                 passwd=db_config.get('password', ''),
                                                 db=db_config.get('database', 'test'),
                                                 charset=db_config.get('charset', 'utf8'))
            cls.conn[db_label].autocommit(cls.autocommit)
        cls.db_config.update(databases)

    @classmethod
    def get_conn(cls, db_label):
        if not cls.conn[db_label] or not cls.conn[db_label].open:
            cls.connect(**cls.db_config)
        try:
            cls.conn[db_label].ping()
        except MySQLdb.OperationalError:
            cls.connect(**cls.db_config)
        return cls.conn[db_label]

    @classmethod
    def execute(cls, db_label, *args):
        db_conn = cls.get_conn(db_label)
        cursor = db_conn.cursor()
        cursor.execute(*args)
        return cursor

    def __del__(self):
        for _, conn in self.conn:
            if conn and conn.open:
                conn.close()


def execute_raw_sql(db_label, sql, params=None):
    return Database.execute(db_label, sql, params) if params else Database.execute(db_label, sql)
