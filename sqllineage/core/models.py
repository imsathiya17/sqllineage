import warnings
from typing import Dict, List, Optional, Set, Union

from sqlparse import tokens as T
from sqlparse.engine import grouping
from sqlparse.sql import (
    Case,
    Comparison,
    Function,
    Identifier,
    IdentifierList,
    Operation,
    Parenthesis,
    Token,
    TokenList,
)
from sqlparse.utils import imt

from sqllineage.exceptions import SQLLineageException
from sqllineage.utils.helpers import escape_identifier_name
from sqllineage.utils.sqlparse import get_parameters


class Schema:
    unknown = "<default>"

    def __init__(self, name: str = unknown):
        """
        Data Class for Schema

        :param name: schema name
        """
        self.raw_name = escape_identifier_name(name)

    def __str__(self):
        return self.raw_name.lower()

    def __repr__(self):
        return "Schema: " + str(self)

    def __eq__(self, other):
        return type(self) is type(other) and str(self) == str(other)

    def __hash__(self):
        return hash(str(self))

    def __bool__(self):
        return str(self) != self.unknown


class Table:
    def __init__(self, name: str, schema: Schema = Schema(), **kwargs):
        """
        Data Class for Table

        :param name: table name
        :param schema: schema as defined by :class:`Schema`
        """
        if "." not in name:
            self.schema = schema
            self.raw_name = escape_identifier_name(name)
        else:
            schema_name, table_name = name.rsplit(".", 1)
            if len(schema_name.split(".")) > 2:
                # allow db.schema as schema_name, but a.b.c as schema_name is forbidden
                raise SQLLineageException("Invalid format for table name: %s.", name)
            self.schema = Schema(schema_name)
            self.raw_name = escape_identifier_name(table_name)
            if schema:
                warnings.warn("Name is in schema.table format, schema param is ignored")
        self.alias = kwargs.pop("alias", self.raw_name)

    def __str__(self):
        return f"{self.schema}.{self.raw_name.lower()}"

    def __repr__(self):
        return "Table: " + str(self)

    def __eq__(self, other):
        return type(self) is type(other) and str(self) == str(other)

    def __hash__(self):
        return hash(str(self))

    @staticmethod
    def of(identifier: Identifier):
        # rewrite identifier's get_real_name method, by matching the last dot instead of the first dot, so that the
        # real name for a.b.c will be c instead of b
        dot_idx, _ = identifier._token_matching(
            lambda token: imt(token, m=(T.Punctuation, ".")),
            start=len(identifier.tokens),
            reverse=True,
        )
        real_name = identifier._get_first_name(dot_idx, real_name=True)
        # rewrite identifier's get_parent_name accordingly
        parent_name = (
            "".join(
                [
                    escape_identifier_name(token.value)
                    for token in identifier.tokens[:dot_idx]
                ]
            )
            if dot_idx
            else None
        )
        schema = Schema(parent_name) if parent_name is not None else Schema()
        alias = identifier.get_alias()
        kwargs = {"alias": alias} if alias else {}
        return Table(real_name, schema, **kwargs)


class Partition:
    pass


class SubQuery:
    def __init__(self, token: Parenthesis, alias: Optional[str]):
        """
        Data Class for SubQuery

        :param token: subquery token
        :param alias: subquery name
        """
        self.token = token
        self._query = token.value
        self.alias = alias if alias is not None else f"subquery_{hash(self)}"

    def __str__(self):
        return self.alias

    def __repr__(self):
        return "SubQuery: " + str(self)

    def __eq__(self, other):
        return type(self) is type(other) and self._query == other._query

    def __hash__(self):
        return hash(self._query)

    @staticmethod
    def of(parenthesis: Parenthesis, alias: Optional[str]):
        return SubQuery(parenthesis, alias)


class Column:
    def __init__(self, name: str, **kwargs):
        """
        Data Class for Column

        :param name: column name
        :param parent: :class:`Table` or :class:`SubQuery`
        :param kwargs:
        """
        if "." in name:
            table_name, column_name = name.rsplit(".", 1)
            self._parent: Set[Union[Table, SubQuery]] = {Table(table_name)}
            self.raw_name = escape_identifier_name(column_name)
        else:
            self._parent = set()
            self.raw_name = escape_identifier_name(name)
        self.source_raw_names = kwargs.pop("source_raw_names", ((self.raw_name, None),))

    def __str__(self):
        return (
            f"{self.parent}.{self.raw_name.lower()}"
            if self.parent is not None
            else f"{self.raw_name.lower()}"
        )

    def __repr__(self):
        return "Column: " + str(self)

    def __eq__(self, other):
        return type(self) is type(other) and str(self) == str(other)

    def __hash__(self):
        return hash(str(self))

    @property
    def parent(self) -> Optional[Union[Table, SubQuery]]:
        return list(self._parent)[0] if len(self._parent) == 1 else None

    @parent.setter
    def parent(self, value: Union[Table, SubQuery]):
        self._parent.add(value)

    @property
    def parent_candidates(self) -> List[Union[Table, SubQuery]]:
        return sorted(self._parent, key=lambda p: str(p))

    @staticmethod
    def of(token: Token):
        if isinstance(token, Identifier):
            alias = token.get_alias()
            if alias:
                # handle column alias, including alias for column name or Case, Function
                kw_idx, kw = token.token_next_by(m=(T.Keyword, "AS"))
                if kw_idx is None:
                    # alias without AS
                    kw_idx, _ = token.token_next_by(i=Identifier)
                idx, _ = token.token_prev(kw_idx, skip_cm=True)
                expr = grouping.group(TokenList(token.tokens[: idx + 1]))[0]
                source_raw_names = Column._extract_source_raw_names(expr)
                return Column(alias, source_raw_names=source_raw_names)
            else:
                # select column name directly without alias
                return Column(
                    token.get_real_name(),
                    source_raw_names=(
                        (token.get_real_name(), token.get_parent_name()),
                    ),
                )
        else:
            # Wildcard, Case, Function without alias (thus not recognized as an Identifier)
            source_raw_names = Column._extract_source_raw_names(token)
            return Column(token.value, source_raw_names=source_raw_names)

    @staticmethod
    def _extract_source_raw_names(token: Token):
        if isinstance(token, Function):
            # max(col1) AS col2
            source_raw_names = tuple(
                x
                for tk in get_parameters(token)
                for x in Column._extract_source_raw_names(tk)
            )
        elif isinstance(token, Parenthesis):
            # This is to avoid circular import
            from sqllineage.runner import LineageRunner
            from sqllineage.utils.sqlparse import is_subquery

            if is_subquery(token):
                # (SELECT avg(col1) AS col1 FROM tab3), used after WHEN or THEN in CASE clause
                src_cols = [
                    lineage[0]
                    for lineage in LineageRunner(token.value).get_column_lineage(False)
                ]
                source_raw_names = tuple(
                    (src_col.raw_name, src_col.parent.raw_name) for src_col in src_cols
                )
            else:
                # (PARTITION BY col1 ORDER BY col2 DESC)
                source_raw_names = tuple(
                    x
                    for tk in token.get_sublists()
                    for x in Column._extract_source_raw_names(tk)
                )
        elif isinstance(token, Operation):
            # col1 + col2 AS col3
            source_raw_names = tuple(
                x
                for tk in token.get_sublists()
                for x in Column._extract_source_raw_names(tk)
            )
        elif isinstance(token, Case):
            # CASE WHEN col1 = 2 THEN "V1" WHEN col1 = "2" THEN "V2" END AS col2
            source_raw_names = tuple(
                x
                for tk in token.get_sublists()
                for x in Column._extract_source_raw_names(tk)
            )
        elif isinstance(token, Comparison):
            from_left = Column._extract_source_raw_names(token.left)
            from_right = Column._extract_source_raw_names(token.right)
            source_raw_names = (*from_left, *from_right)
        elif isinstance(token, IdentifierList):
            source_raw_names = tuple(
                x
                for tk in token.get_sublists()
                for x in Column._extract_source_raw_names(tk)
            )
        elif isinstance(token, Identifier):
            if token.get_real_name():
                # col1 AS col2
                source_raw_names = ((token.get_real_name(), token.get_parent_name()),)
            else:
                # col1=1 AS int
                source_raw_names = tuple(
                    x
                    for tk in token.get_sublists()
                    for x in Column._extract_source_raw_names(tk)
                )
        else:
            # Handle literals other than *
            if (
                token.ttype is not None
                and token.ttype[0] == T.Literal[0]
                and token.value != "*"
            ):
                source_raw_names = tuple()
            else:
                # select *
                source_raw_names = ((token.value, None),)
        return source_raw_names

    def to_source_columns(self, alias_mapping: Dict[str, Union[Table, SubQuery]]):
        """
        Best guess for source table given all the possible table/subquery and their alias.
        """

        def _to_src_col(name: str, parent: Union[Table, SubQuery] = None):
            col = Column(name)
            if parent:
                col.parent = parent
            return col

        source_columns = set()
        for (src_col, src_tbl) in self.source_raw_names:
            if src_tbl is None:
                if src_col == "*":
                    # select *
                    for table in set(alias_mapping.values()):
                        source_columns.add(_to_src_col(src_col, table))
                else:
                    # select column without specifying table prefix
                    src_col = _to_src_col(src_col, None)
                    for table in set(alias_mapping.values()):
                        # in case of only one table, we get the right answer
                        # in case of multiple tables, a bunch of possible tables are set
                        src_col.parent = table
                    source_columns.add(src_col)
            else:
                if alias_mapping.get(src_tbl):
                    source_columns.add(_to_src_col(src_col, alias_mapping.get(src_tbl)))
                else:
                    source_columns.add(_to_src_col(src_col, Table(src_tbl)))
        return source_columns
