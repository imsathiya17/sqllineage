from sqlparse.sql import Identifier, IdentifierList, Token

from sqllineage.core.handlers.base import NextTokenBaseHandler
from sqllineage.core.holders import SubQueryLineageHolder
from sqllineage.core.models import SubQuery
from sqllineage.exceptions import SQLLineageException


class CTEHandler(NextTokenBaseHandler):
    CTE_TOKENS = ("WITH",)

    def _indicate(self, token: Token) -> bool:
        return token.normalized in self.CTE_TOKENS

    def _handle(self, token: Token, holder: SubQueryLineageHolder) -> None:
        if isinstance(token, Identifier):
            cte = [token]
        elif isinstance(token, IdentifierList):
            cte = [token for token in token.tokens if isinstance(token, Identifier)]
        else:
            raise SQLLineageException(
                "An Identifier or IdentifierList is expected, got %s[value: %s] instead."
                % (type(token).__name__, token)
            )
        for token in cte:
            sublist = list(token.get_sublists())
            if sublist:
                # CTE: tbl AS (SELECT 1), tbl is alias and (SELECT 1) is subquery Parenthesis
                holder.add_cte(SubQuery.of(sublist[0], token.get_real_name()))
