
from copy import copy
import datetime
from functools import partial
import sys
from typing import TYPE_CHECKING, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Type, Union
from pathlib import Path
import typing

from redbird.oper import Between, In, Operation, skip
from redbird.packages import sqlalchemy, import_exists

from pydantic import BaseModel


try:
    from typing import Literal
except ImportError: # pragma: no cover
    from typing_extensions import Literal

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine
    from sqlalchemy import Column


class Table:
    """SQL Table

    Similar to ``sqlalchemy.Table`` except this has methods
    to make certain operations more intuitive.

    Attributes
    ----------
    table : str
        Name of the table
    engine : sqlalchemy.engine.Engine
        SQLAlchemy engine for the connection.
    
    Examples
    --------
    .. code-block:: python

        import sqlalchemy
        from redbird.sql import Table

        table = Table(engine=sqlalchemy.create_engine("sqlite://"), table="mytable")
    """
    engine: 'Engine'

    types = {
        str: sqlalchemy.String,
        int: sqlalchemy.Integer,
        float: sqlalchemy.Float,
        bool: sqlalchemy.Boolean,
        datetime.date: sqlalchemy.Date,
        datetime.datetime: sqlalchemy.DateTime,
        datetime.timedelta: sqlalchemy.Interval,
        dict: sqlalchemy.JSON,
    } if import_exists("sqlalchemy") else {}
    _name: str
    _object: sqlalchemy.Table

    class _trans_ctx(object):
        def __init__(self, obj:'Table'):
            self.obj = obj

        def __enter__(self):
            new_table = copy(self.obj)
            new_table._ctx = None
            self._ctx = new_table.engine.begin()
            new_table.engine = self._ctx.__enter__()
            return new_table

        def __exit__(self, type_, value, traceback):
            self._ctx.__exit__(type_, value, traceback)

    def __init__(self, table:str, engine:'sqlalchemy.engine.Engine'):
        self.engine = engine
        self.name = table

    def select(self, qry:Union[str, dict, 'sqlalchemy.sql.ClauseElement', None]=None, columns:Optional[List[str]]=None) -> Iterable[dict]:
        """Read the database table using a query
        
        Parameters
        ----------
        qry : str, sqlalchemy.sql.ClauseElement, optional
            Query to filter the data. The argument can take various forms:

            - ``str``: Query is considered to be raw SQL
            - ``dict``: Query is considered to be column-filter pairs.
              The pairs are combined using AND operator. If the filter
              is Operation, it is turned to corresponing SQLAlchemy expression.
              Else, the filter is considered to be an equal operator.
            - sqlalchemy expression: The query is considered to be the *where*
              clause of the select query. 

            If not given, all rows are returned.
        columns: list of string, optional
            List of columns to return. By default returns all columns.

        Returns
        -------
        Generator of dicts
            Found rows as dicts.

        Examples
        --------
        Select all rows:
        
        .. code-block:: python

            table.select("select * from mytable")
        
        Select using raw SQL:
        
        .. code-block:: python

            table.select("select * from mytable")

        Select where ``column_1 = "a value" and column_2 = 10``:

        .. code-block:: python

            table.select({"column_1": "a value", "column_2": 10})

        Select where ``column_1 = "a value" and column_2 = 10``:

        .. code-block:: python

            from sqlalchemy import Column
            table.select((Column("column_1") == "a value") & (Column("column_2") == 10))

        Select where ``column_1 = "a value" and column_2 = 10`` but include only the column 
        ``column_1`` in the output:

        .. code-block:: python

            table.select({"column_1": "a value", "column_2": 10}, columns=["column_1"])
        """
        if isinstance(qry, Path):
            qry = qry.read_text()
        elif qry is None:
            qry = sqlalchemy.true()

        if isinstance(qry, dict):
            qry = self.to_sql_expressions(qry)
        if isinstance(qry, str):
            statement = sqlalchemy.text(qry)
        else:
            if columns is None:
                columns = ()
            else:
                columns = [
                    self.object.columns[col]
                    for col in columns
                ]
            where = qry
            statement = self.object.select(*columns)
            statement = statement.where(where)
            statement = statement.select_from(self.object)
        results = self.execute(statement)
        rows = results.mappings()
        if self.name is not None:
            return self._format_results(rows)
        return rows
    
    def insert(self, data):
        table = self.object
        statement = table.insert().values(**data)
        self.execute(statement)

    def delete(self, where:Union[dict, 'sqlalchemy.sql.ClauseElement']):
        """Delete row(s) from the table
        
        Parameters
        ----------
        where : dict, sqlalchemy expression
            Where clause to delete data.

        Examples
        --------

        Delete where ``column_1 = "a" and column_2 = 1``:

        .. code-block:: python

            table.delete({"column_1": "a", "column_2": 1})

        Delete where ``column_1 = "a" and column_2 = 1``:

        .. code-block:: python

            from sqlalchemy import Column
            table.delete((Column("column_1") == "a") & (Column("column_2") == 1))

        """
        if isinstance(where, dict):
            where = self.to_sql_expressions(where)
        table = self.object
        statement = table.delete().where(where)
        self.execute(statement)

    def update(self, where:Union[dict, 'sqlalchemy.sql.ClauseElement'], values):
        """Update row(s) in the table
        
        Parameters
        ----------
        where : dict, sqlalchemy expression
            Where clause to update rows.
        values : dict
            Column-value pairs to update the 
            rows matching the where clause.

        Examples
        --------

        Set ``column_3`` to ``"new value"`` where ``column_1 = "a" and column_2 = 1``:

        .. code-block:: python

            table.delete({"column_1": "a", "column_2": 1}, {"column_3": "new value"})

        Set ``column_3`` to ``"new value"`` where ``column_1 = "a" and column_2 = 1``:

        .. code-block:: python

            from sqlalchemy import Column
            table.delete((Column("column_1") == "a") & (Column("column_2") == 1), {"column_3": "new value"})

        """
        if isinstance(where, dict):
            where = self.to_sql_expressions(where)
        table = self.object
        statement = table.update().where(where).values(values)
        self.execute(statement)

    def count(self, where=None) -> int:
        """Count the number of rows
        
        Parameters
        ----------
        where : dict, sqlalchemy expression, optional
            Where clause to be satisfied for counting the rows.

        Returns
        -------
        int
            Count of rows (satisfying the where clause).

        Examples
        --------

        Count rows where ``column_1 = "a" and column_2 = 1``:

        .. code-block:: python

            table.count({"column_1": "a", "column_2": 1}, {"column_3": "new value"})
        """
        stmt = sqlalchemy.select(sqlalchemy.func.count()).select_from(self.object)
        if where is not None:
            if isinstance(where, dict):
                where = self.to_sql_expressions(where)
            stmt = stmt.where(where)
        return list(self.execute(stmt))[0][0]

    def _format_results(self, res:Iterable[Tuple]) -> Iterable[dict]:
        columns = self._get_types()
        for row in res:
            row = {name: conv(row[name]) for name, conv in columns.items()}
            yield row

    def _get_types(self) -> Dict[str, Callable]:
        "Get table's column types"
        cols = dict(self.object.columns)
        return {col_name: partial(to_native, sql_type=col.type, nullable=col.nullable) for col_name, col in cols.items()}

    def to_sql_expressions(self, qry:dict, table=None):
        stmt = sqlalchemy.true()
        for column_name, oper_or_value in qry.items():
            column = getattr(table, column_name) if table is not None else sqlalchemy.Column(column_name)
            if isinstance(oper_or_value, Operation):
                oper = oper_or_value
                if isinstance(oper, Between):
                    sql_oper = column.between(oper.start, oper.end)
                elif isinstance(oper, In):
                    sql_oper = column.in_(oper.value)
                elif oper is skip:
                    continue
                elif hasattr(oper, "__py_magic__"):
                    magic = oper.__py_magic__
                    oper_method = getattr(column, magic)

                    # Here we form the SQLAlchemy operation, ie.: column("mycol") >= 5
                    sql_oper = oper_method(oper.value)
                else:
                    raise NotImplementedError(f"Not implemented operator: {oper}")
            else:
                value = oper_or_value
                sql_oper = column == value
            stmt &= sql_oper
        return stmt

    def _to_sqlalchemy_type(self, cls):
        from sqlalchemy.sql import sqltypes
        if isinstance(cls, sqltypes.TypeEngine):
            return cls
        is_older_py = sys.version_info < (3, 8)
        origin = typing.get_origin(cls) if not is_older_py else None
        if origin is not None:
            # In form: 
            # - Literal['', '']
            # - Optional[...]
            args = typing.get_args(cls)
            if origin is typing.Union:
                # Either:
                # - Union[...]
                # - Optional[...]
                # Only Union[<TYPE>, NoneType] is allowed
                none_type = type(None)
                has_none_type = none_type in args
                if len(args) > 2 or (len(args) == 2 and not has_none_type):
                    raise TypeError(f"Union has more than one optional type: {str(cls)}. Cannot define SQL data type")
                # Get the non-None type
                for arg in args:
                    if arg is not none_type:
                        cls = arg
                        break
            
            if origin is Literal:
                type_ = type(args[0])
                for arg in args[1:]:
                    if not isinstance(arg, type_):
                        raise TypeError(f"Literal values are not same types: {str(cls)}. Cannot define SQL data type")
                cls = type_
        return self.types.get(cls)

    def reflect(self):
        self._object = reflect_table(self.name, engine=self.engine)

    def create(self, columns:Union[List['Column'], Mapping[str, Type]]):
        """Create the table
        
        Parameters
        ----------
        columns : dict, list of sqlalchemy.Column, dict or string
            Columns to be created.

        Examples
        --------

        Create a table with columns ``column_1``, ``column_2`` and ``column_3``
        (all of them all text): 

        .. code-block:: python

            table.create(["column_1", "column_2", "column_3"])

        Create a table with columns ``column_1``, ``column_2`` and ``column_3``
        with varying data types: 

        .. code-block:: python

            import datetime
            table.create({"column_1": str, "column_2": int, "column_3": datetime.datetime})

        Create a table with columns ``column_1``, ``column_2`` and ``column_3``
        using SQLAlchemy columns: 

        .. code-block:: python

            from sqlalchemy import Column, String, Integer, DateTime
            table.create([
                Column("column_1", type_=String()),
                Column("column_2", type_=Integer()),
                Column("column_3", type_=DateTime())
            ])

        Create a table with columns ``column_1``, ``column_2`` and ``column_3``
        using list of dicts: 

        .. code-block:: python

            from sqlalchemy import DateTime
            table.create([
                {"name": "column_1", "type_": str},
                {"name": "column_2", "type_": int},
                {"name": "column_3", "type_": DateTime()},
            ])
        """
        if isinstance(columns, Mapping):
            columns = [
                sqlalchemy.Column(name, self._to_sqlalchemy_type(type_))
                for name, type_ in columns.items()
            ]
        elif isinstance(columns, (list, tuple)):
            columns = [
                sqlalchemy.Column(col, type_=sqlalchemy.String()) if isinstance(col, str) 
                else sqlalchemy.Column(
                    type_=self._to_sqlalchemy_type(col['type_']), 
                    **{k: v for k, v in col.items() if k not in ('type_',)}
                ) if isinstance(col, dict)
                else col
                for col in columns
            ]
        meta = sqlalchemy.MetaData()
        tbl = sqlalchemy.Table(self.name, meta, *columns)
        tbl.create(bind=self.engine)
        self._object = tbl

    def create_from_model(self, model:BaseModel, primary_column=None):
        if isinstance(primary_column, str):
            primary_column = (primary_column,)
        sql_cols = [
            sqlalchemy.Column(
                name, 
                self._to_sqlalchemy_type(field.type_), 
                primary_key=name in primary_column if primary_column is not None else False, 
                nullable=not field.required,
                default=field.default
            )
            for name, field in model.__fields__.items()
        ]
        self.create(sql_cols)
  
    def execute(self, *args, **kwargs):
        if len(args) == 1 and isinstance(args[0], str):
            # SQLAlchemy v2.0 won't accept string as 
            # expression but we do
            args = (sqlalchemy.text(*args),)
        conn = self.connection
        if isinstance(conn, sqlalchemy.engine.Engine):
            with conn.begin() as conn:
                return conn.execute(*args, **kwargs)
        else:
            return conn.execute(*args, **kwargs)

    def open_transaction(self):
        self._ctx = self._trans_ctx(self)
        return self._ctx.__enter__()

    def __enter__(self):
        """Open a transaction.
        
        Examples
        --------
        .. code-block:: python

            from redbird.sql import Table
            from sqlalchemy import create_engine

            table = Table(engine=create_engine(...), table="mytable")

            with table as transaction:
                transaction.insert({"col_1": "a", "col_2": "b"})
                transaction.delete({"col_2": "c"})
        """
        self._ctx = self._trans_ctx(self)
        return self._ctx.__enter__()

    def __exit__(self, type_, value, traceback):
        self._ctx.__exit__(type_, value, traceback)

    def rollback(self):
        "Rollback the open transaction (a transaction must be open)"
        return self.connection.get_transaction().rollback()

    def commit(self):
        "Commit the open transaction (a transaction must be open)"
        return self.connection.get_transaction().commit()

    @property
    def connection(self) -> 'sqlalchemy.engine.Connection':
        "Synonym to engine"
        return self.engine

    @connection.setter
    def connection(self, value):
        self.engine = value

    @property
    def object(self):
        "SQLAlchemy representation of the table"
        if self._object is None:
            self.reflect()
        return self._object

    @object.setter
    def object(self, value: sqlalchemy.Table):
        self._object = value
        self._name = value.name

    @property
    def name(self):
        if self._object is not None:
            return self._object.name
        return self._name
    
    @name.setter
    def name(self, value: str):
        self._name = value
        self._object = None


def to_native(value, sql_type, nullable=False):
    "Convert sql type to Python native"
    py_type = sql_type.python_type
    if isinstance(value, py_type):
        return value
    elif py_type is datetime.datetime and isinstance(value, str):
        return datetime.datetime.fromisoformat(value)
    elif py_type is datetime.date and isinstance(value, str):
        return datetime.date.fromisoformat(value)
    if nullable and value is None:
        return value
    return py_type(value)

def create_table(table, columns, engine):
    return Table(engine=engine, table=table).create(columns)

def reflect_table(table:str, *columns, engine:'Engine', meta=None):
    """Reflect a table in an SQL database"""
    if meta is None:
        meta = sqlalchemy.MetaData()
    return sqlalchemy.Table(table, meta, *columns, autoload_with=engine)

def select(*args, engine:'Engine', table:str=None, **kwargs):
    """Read rows from a table in a SQL database
    
    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
        SQLAlchemy engine for the connection
    table : str
        Name of the table to use.
    *args
        Passed to :py:meth:`redbird.sql.Table.select`
    **kwargs
        Passed to :py:meth:`redbird.sql.Table.select`
    """
    return Table(engine=engine, table=table).select(*args, **kwargs)

def insert(*args, engine:'Engine', table:str=None, **kwargs):
    """Insert row(s) to a table in a SQL database

    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
        SQLAlchemy engine for the connection
    table : str
        Name of the table to use.
    *args
        Passed to :py:meth:`redbird.sql.Table.insert`
    **kwargs
        Passed to :py:meth:`redbird.sql.Table.insert`
    """
    return Table(engine=engine, table=table).insert(*args, **kwargs)

def delete(*args, engine:'Engine', table:str=None, **kwargs):
    """Delete row(s) in a table in a SQL database
    
    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
        SQLAlchemy engine for the connection
    table : str
        Name of the table to use.
    *args
        Passed to :py:meth:`redbird.sql.Table.delete`
    **kwargs
        Passed to :py:meth:`redbird.sql.Table.delete`
    """
    return Table(engine=engine, table=table).delete(*args, **kwargs)

def update(*args, engine:'Engine', table:str=None, **kwargs):
    """Update row(s) to a table in a SQL database
    
    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
        SQLAlchemy engine for the connection
    table : str
        Name of the table to use.
    *args
        Passed to :py:meth:`redbird.sql.Table.update`
    **kwargs
        Passed to :py:meth:`redbird.sql.Table.update`
    """
    return Table(engine=engine, table=table).update(*args, **kwargs)

def count(*args, engine:'Engine', table:str=None, **kwargs):
    """Count the number of rows in a table in a SQL database
    
    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
        SQLAlchemy engine for the connection
    table : str
        Name of the table to use.
    *args
        Passed to :py:meth:`redbird.sql.Table.count`
    **kwargs
        Passed to :py:meth:`redbird.sql.Table.count`
    """
    return Table(engine=engine, table=table).count(*args, **kwargs)

def execute(*args, engine:'Engine', **kwargs):
    """Execute raw SQL or a SQL expression in a SQL database
    
    Parameters
    ----------
    engine : sqlalchemy.engine.Engine
        SQLAlchemy engine for the connection
    table : str
        Name of the table to use.
    *args
        Passed to :py:meth:`redbird.sql.Table.execute`
    **kwargs
        Passed to :py:meth:`redbird.sql.Table.execute`
    """
    return Table(engine=engine, table=None).execute(*args, **kwargs)
