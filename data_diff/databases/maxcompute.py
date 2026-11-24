import logging
from typing import Any, ClassVar, Dict, Optional, Tuple, Type

import attrs

from data_diff.abcs.database_types import (
    Boolean,
    ColType,
    Date,
    DbPath,
    DbTime,
    Decimal,
    Float,
    FractionalType,
    Integer,
    TemporalType,
    Text,
    Timestamp,
)
from data_diff.databases.base import (
    BaseDialect,
    Database,
    import_helper,
)
from data_diff.schema import RawColumnInfo
from data_diff.utils import match_regexps

logger = logging.getLogger("database.maxcompute")


@import_helper("maxcompute")
def import_maxcompute():
    import odps

    return odps


class Dialect(BaseDialect):
    name = "MaxCompute"
    ROUNDS_ON_PREC_LOSS = True
    TYPE_CLASSES = {
        # Timestamps / Dates
        "datetime": Timestamp,
        "timestamp": Timestamp,
        "date": Date,
        # Numbers
        "tinyint": Integer,
        "smallint": Integer,
        "int": Integer,
        "integer": Integer,
        "bigint": Integer,
        "float": Float,
        "double": Float,
        "decimal": Decimal,
        # Text
        "string": Text,
        "varchar": Text,
        "char": Text,
        # Boolean
        "boolean": Boolean,
    }

    def quote(self, s: str) -> str:
        return f"`{s}`"

    def to_string(self, s: str) -> str:
        return f"cast({s} as string)"

    def type_repr(self, t) -> str:
        try:
            return {float: "double", int: "bigint", str: "string", bool: "boolean"}[t]
        except KeyError:
            return super().type_repr(t)

    def timestamp_value(self, t: DbTime) -> str:
        return f"datetime '{t.strftime('%Y-%m-%d %H:%M:%S')}'"

    def parse_type(self, table_path: DbPath, info: RawColumnInfo) -> ColType:
        dtype = info.data_type.lower()
        number_regexps = {r"decimal\((\d+),(\d+)\)": Decimal}
        for m, n_cls in match_regexps(number_regexps, dtype):
            _prec, scale = map(int, m.groups())
            return n_cls(scale)

        string_regexps = {r"varchar\((\d+)\)": Text, r"char\((\d+)\)": Text}
        for m, n_cls in match_regexps(string_regexps, dtype):
            return n_cls()

        return super().parse_type(table_path, info)

    def set_timezone_to_utc(self) -> str:
        return "SET odps.sql.timezone=UTC"

    def current_timestamp(self) -> str:
        return "GETDATE()"

    def md5_as_int(self, s: str) -> str:
        return f"cast(conv(substr(md5(cast({s} as string)), 1, 15), 16, 10) as decimal(38, 0))"

    def md5_as_hex(self, s: str) -> str:
        return f"md5(cast({s} as string))"

    def normalize_timestamp(self, value: str, coltype: TemporalType) -> str:
        return f"to_char({value}, 'yyyy-mm-dd hh:mi:ss')"

    def normalize_number(self, value: str, coltype: FractionalType) -> str:
        return self.to_string(f"cast({value} as decimal(38, {coltype.precision}))")

    def normalize_boolean(self, value: str, _coltype: Boolean) -> str:
        return self.to_string(f"case when {value} then 1 else 0 end")


@attrs.define(frozen=False, init=False, kw_only=True)
class MaxCompute(Database):
    DIALECT_CLASS: ClassVar[Type[BaseDialect]] = Dialect
    CONNECT_URI_HELP = "odps://<access_id>:<access_key>@<endpoint>/<project>"
    CONNECT_URI_PARAMS = ["project"]
    CONNECT_URI_KWPARAMS = []

    project: str
    access_id: Optional[str] = None
    access_key: Optional[str] = None
    endpoint: Optional[str] = None
    allow_fullscan: bool = False
    _odps: Any = None

    def __init__(self, **kw) -> None:
        super().__init__()
        self.project = kw.get("project") or ""
        self.access_id = kw.get("user") or kw.get("access_id") or ""
        self.access_key = kw.get("password") or kw.get("access_key") or ""
        self.allow_fullscan = kw.get("allow_fullscan", False)

        endpoint = kw.get("endpoint")
        if not endpoint and kw.get("host"):
            host = kw["host"]
            port = kw.get("port")
            if host.startswith("http://") or host.startswith("https://"):
                endpoint = host
            else:
                proto = "https" if port == 443 else "http"
                endpoint = f"{proto}://{host}"
                if port and port not in (80, 443):
                    endpoint += f":{port}"
                endpoint += "/api"
        self.endpoint = endpoint

        odps = import_maxcompute()
        init_args: Dict[str, Any] = {
            "access_id": self.access_id,
            "secret_access_key": self.access_key,
            "project": self.project,
        }
        if self.endpoint:
            init_args["endpoint"] = self.endpoint

        self._odps = odps.ODPS(**init_args)

    def _query(self, sql_code: str) -> list:
        hints = {}
        if self.allow_fullscan:
            hints["odps.sql.allow.fullscan"] = "true"
        instance = self._odps.execute_sql(sql_code, hints=hints)
        with instance.open_reader() as reader:
            return [tuple(record.values) for record in reader]

    def close(self):
        super().close()
        self._odps = None

    def _normalize_project_table(self, path: DbPath) -> Tuple[str, str]:
        if len(path) == 1:
            return self.project, path[0]
        elif len(path) == 2:
            return path[0], path[1]
        raise ValueError(f"{self.name}: Bad table path '{'.'.join(path)}'. Expected [project.]table")

    def select_table_schema(self, path: DbPath) -> str:
        project, table_name = self._normalize_project_table(path)
        return f"DESCRIBE `{project}`.`{table_name}`"

    def query_table_schema(self, path: DbPath) -> Dict[str, RawColumnInfo]:
        if self._odps is not None:
            try:
                project, table_name = self._normalize_project_table(path)
                t = self._odps.get_table(table_name, project=project)
                schema = t.table_schema
                result = {}
                for col in schema.columns:
                    name = col.name
                    col_type = col.type.name.lower() if hasattr(col.type, "name") else str(col.type).lower()
                    num_prec = getattr(col.type, "precision", None)
                    num_scale = getattr(col.type, "scale", None)
                    result[name] = RawColumnInfo(
                        column_name=name,
                        data_type=col_type,
                        datetime_precision=3 if "time" in col_type else None,
                        numeric_precision=num_prec,
                        numeric_scale=num_scale,
                    )
                for col in schema.partitions:
                    name = col.name
                    col_type = col.type.name.lower() if hasattr(col.type, "name") else str(col.type).lower()
                    result[name] = RawColumnInfo(
                        column_name=name,
                        data_type=col_type,
                        datetime_precision=None,
                        numeric_precision=None,
                        numeric_scale=None,
                    )
                return result
            except Exception as e:
                logger.debug(f"ODPS get_table metadata lookup failed, falling back to SQL: {e}")
        return super().query_table_schema(path)

    @property
    def is_autocommit(self) -> bool:
        return True
