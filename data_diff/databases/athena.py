import logging
import re
from typing import Any, ClassVar, Dict, Optional, Tuple, Type

import attrs

from data_diff.abcs.database_types import (
    Boolean,
    ColType,
    ColType_UUID,
    DbPath,
    DbTime,
    Decimal,
    Float,
    FractionalType,
    Integer,
    TemporalType,
    Text,
    Timestamp,
    TimestampTZ,
)
from data_diff.databases.base import (
    BaseDialect,
    Database,
    import_helper,
    TIMESTAMP_PRECISION_POS,
)
from data_diff.schema import RawColumnInfo
from data_diff.utils import match_regexps

logger = logging.getLogger("database.athena")


@import_helper("athena")
def import_athena():
    import pyathena

    return pyathena


@import_helper("athena")
def import_boto3():
    import boto3

    return boto3


class Dialect(BaseDialect):
    name = "Athena"
    ROUNDS_ON_PREC_LOSS = True
    TYPE_CLASSES = {
        # Timestamps
        "timestamp with time zone": TimestampTZ,
        "timestamp without time zone": Timestamp,
        "timestamp": Timestamp,
        # Numbers
        "integer": Integer,
        "int": Integer,
        "bigint": Integer,
        "smallint": Integer,
        "tinyint": Integer,
        "real": Float,
        "float": Float,
        "double": Float,
        # Text
        "varchar": Text,
        "string": Text,
        "char": Text,
        # Boolean
        "boolean": Boolean,
    }

    def quote(self, s: str) -> str:
        return f'"{s}"'

    def to_string(self, s: str) -> str:
        return f"cast({s} as varchar)"

    def type_repr(self, t) -> str:
        if isinstance(t, TimestampTZ):
            return "timestamp with time zone"
        try:
            return {float: "double", int: "bigint", str: "varchar", bool: "boolean"}[t]
        except KeyError:
            return super().type_repr(t)

    def timestamp_value(self, t: DbTime) -> str:
        return f"timestamp '{t.isoformat(' ')}'"

    def parse_type(self, table_path: DbPath, info: RawColumnInfo) -> ColType:
        dtype = info.data_type.lower()
        timestamp_regexps = {
            r"timestamp\((\d)\)": Timestamp,
            r"timestamp\((\d)\) with time zone": TimestampTZ,
        }
        for m, t_cls in match_regexps(timestamp_regexps, dtype):
            precision = int(m.group(1))
            return t_cls(precision=precision, rounds=self.ROUNDS_ON_PREC_LOSS)

        number_regexps = {r"decimal\((\d+),(\d+)\)": Decimal}
        for m, n_cls in match_regexps(number_regexps, dtype):
            _prec, scale = map(int, m.groups())
            return n_cls(scale)

        string_regexps = {r"varchar\((\d+)\)": Text, r"char\((\d+)\)": Text}
        for m, n_cls in match_regexps(string_regexps, dtype):
            return n_cls()

        return super().parse_type(table_path, info)

    def set_timezone_to_utc(self) -> str:
        return "SET TIME ZONE '+00:00'"

    def current_timestamp(self) -> str:
        return "current_timestamp"

    def md5_as_int(self, s: str) -> str:
        return f"from_base(substr(to_hex(md5(to_utf8(cast({s} as varchar)))), 1, 15), 16)"

    def md5_as_hex(self, s: str) -> str:
        return f"to_hex(md5(to_utf8(cast({s} as varchar))))"

    def normalize_uuid(self, value: str, coltype: ColType_UUID) -> str:
        return f"TRIM(cast({value} as varchar))"

    def normalize_timestamp(self, value: str, coltype: TemporalType) -> str:
        if coltype.precision > 0:
            s = f"date_format(cast({value} as timestamp(6)), '%Y-%m-%d %H:%i:%S.%f')"
            return (
                f"RPAD(RPAD({s}, {TIMESTAMP_PRECISION_POS + coltype.precision}, '.'), "
                f"{TIMESTAMP_PRECISION_POS + 6}, '0')"
            )
        return f"date_format(cast({value} as timestamp), '%Y-%m-%d %H:%i:%S')"

    def normalize_number(self, value: str, coltype: FractionalType) -> str:
        return self.to_string(f"cast({value} as decimal(38,{coltype.precision}))")

    def normalize_boolean(self, value: str, _coltype: Boolean) -> str:
        return self.to_string(f"case when {value} then 1 else 0 end")


@attrs.define(frozen=False, init=False, kw_only=True)
class Athena(Database):
    DIALECT_CLASS: ClassVar[Type[BaseDialect]] = Dialect
    CONNECT_URI_HELP = (
        "athena://[aws_key:aws_secret@]athena.<region>.amazonaws.com/<catalog>/<schema>?s3_staging_dir=...&work_group=..."
    )
    CONNECT_URI_PARAMS = ["catalog", "schema"]
    CONNECT_URI_KWPARAMS = []

    default_catalog: str = "AwsDataCatalog"
    default_schema: str = "default"
    s3_staging_dir: Optional[str] = None
    work_group: Optional[str] = "primary"
    region_name: Optional[str] = None
    use_glue_schema: bool = True
    last_query_stats: Dict[str, Any] = attrs.field(factory=dict)
    _conn: Any = None

    def __init__(self, **kw) -> None:
        super().__init__()
        self.default_catalog = kw.get("catalog") or kw.get("catalog_name") or "AwsDataCatalog"
        self.default_schema = kw.get("schema") or kw.get("schema_name") or kw.get("database") or "default"
        self.s3_staging_dir = kw.get("s3_staging_dir")
        self.work_group = kw.get("work_group") or kw.get("workgroup") or "primary"
        self.use_glue_schema = kw.get("use_glue_schema", True)
        self.last_query_stats = {}

        # Resolve region from kw or host (e.g. athena.us-east-1.amazonaws.com)
        region = kw.get("region_name") or kw.get("region")
        if not region and kw.get("host"):
            m = re.search(r"athena\.([a-z0-9-]+)\.amazonaws\.com", kw["host"], re.IGNORECASE)
            if m:
                region = m.group(1)
        self.region_name = region

        connect_args: Dict[str, Any] = {
            "s3_staging_dir": self.s3_staging_dir,
            "region_name": self.region_name,
            "work_group": self.work_group,
            "catalog_name": self.default_catalog,
            "schema_name": self.default_schema,
        }

        # IAM credentials / auth
        if kw.get("user"):
            connect_args["aws_access_key_id"] = kw["user"]
        elif kw.get("aws_access_key_id"):
            connect_args["aws_access_key_id"] = kw["aws_access_key_id"]

        if kw.get("password"):
            connect_args["aws_secret_access_key"] = kw["password"]
        elif kw.get("aws_secret_access_key"):
            connect_args["aws_secret_access_key"] = kw["aws_secret_access_key"]

        if kw.get("aws_session_token"):
            connect_args["aws_session_token"] = kw["aws_session_token"]

        if kw.get("profile_name"):
            connect_args["profile_name"] = kw["profile_name"]

        if kw.get("role_arn"):
            connect_args["role_arn"] = kw["role_arn"]

        # Only connect if pyathena is invoked and credentials/staging provided
        pyathena = import_athena()
        filtered_args = {k: v for k, v in connect_args.items() if v is not None}
        self._conn = pyathena.connect(**filtered_args)

    def _query(self, sql_code: str) -> list:
        cursor = self._conn.cursor()
        cursor.execute(sql_code)
        self.last_query_stats = {
            "data_scanned_in_bytes": getattr(cursor, "data_scanned_in_bytes", 0),
            "query_id": getattr(cursor, "query_id", None),
            "execution_time_in_millis": getattr(cursor, "execution_time_in_millis", None),
        }
        if cursor.description:
            return cursor.fetchall()
        return []

    def close(self):
        super().close()
        if self._conn:
            self._conn.close()

    def _normalize_table_path(self, path: DbPath) -> Tuple[str, str]:
        if len(path) == 1:
            return self.default_schema, path[0]
        elif len(path) == 2:
            return path[0], path[1]
        elif len(path) == 3:
            return path[1], path[2]
        raise ValueError(
            f"{self.name}: Bad table path '{'.'.join(path)}'. Expected [catalog.]schema.table or table"
        )

    def select_table_schema(self, path: DbPath) -> str:
        schema, table = self._normalize_table_path(path)
        return (
            "SELECT column_name, data_type, 3 as datetime_precision, 3 as numeric_precision, NULL as numeric_scale "
            "FROM information_schema.columns "
            f"WHERE table_name = '{table}' AND table_schema = '{schema}'"
        )

    def _query_glue_schema(self, path: DbPath) -> Dict[str, RawColumnInfo]:
        boto3 = import_boto3()
        glue = boto3.client("glue", region_name=self.region_name)
        schema, table_name = self._normalize_table_path(path)
        res = glue.get_table(DatabaseName=schema, Name=table_name)
        tbl = res.get("Table", {})
        columns = tbl.get("StorageDescriptor", {}).get("Columns", []) + tbl.get("PartitionKeys", [])
        if not columns:
            raise RuntimeError(f"Athena/Glue: Table '{schema}.{table_name}' has no columns.")

        result = {}
        for col in columns:
            name = col["Name"]
            raw_type = col["Type"].lower()
            dt_prec = 3 if "timestamp" in raw_type else None
            num_prec = None
            num_scale = None
            m = re.match(r"decimal\((\d+),(\d+)\)", raw_type)
            if m:
                num_prec = int(m.group(1))
                num_scale = int(m.group(2))
            result[name] = RawColumnInfo(
                column_name=name,
                data_type=raw_type,
                datetime_precision=dt_prec,
                numeric_precision=num_prec,
                numeric_scale=num_scale,
            )
        return result

    def query_table_schema(self, path: DbPath) -> Dict[str, RawColumnInfo]:
        if self.use_glue_schema:
            try:
                return self._query_glue_schema(path)
            except Exception as e:
                logger.debug(f"Glue get_table failed, falling back to SQL: {e}")
        return super().query_table_schema(path)

    @property
    def is_autocommit(self) -> bool:
        return True
