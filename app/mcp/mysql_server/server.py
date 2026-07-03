"""MySQL 查询 MCP 服务器（stdio）。

供 `/api/agent/mcp_service_recommendation` 端点连接，让推荐 Agent 通过
`execute_sql` 工具查询 ioeb 服务库（services / service_apis / service_api_tools）。

数据库连接从环境变量读取，优先 MYSQL_*，回退到 ioeb_backend 的 DB_* 命名：
    MYSQL_HOST     / DB_HOST
    MYSQL_PORT     / DB_PORT
    MYSQL_USER     / DB_USERNAME
    MYSQL_PASSWORD / DB_PASSWORD
    MYSQL_DATABASE / DB_NAME

只读为主：SELECT/SHOW 返回 JSON 行；非查询语句会执行并提交，但推荐场景只用 SELECT。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# 部署（ioeb/docker-compose.yml agent volumes）：
#   env/micro-agent.env -> /app/.env
#   env/mysql.env       -> app/mcp/mysql_server/.env
# 本地无 mysql.env 时 load_dotenv 无操作；有 Micro-Agent/.env 即可联调。
# override=False：stdio 父进程已传入的 DB_* 优先。
_APP_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(_APP_ROOT / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("mysql_mcp_server")

mcp = FastMCP("mysql_mcp_server")


def _env(*names: str, default: str | None = None) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v not in (None, ""):
            return v
    return default


def get_db_config() -> dict:
    cfg = {
        "host": _env("MYSQL_HOST", "DB_HOST", default="localhost"),
        "port": int(_env("MYSQL_PORT", "DB_PORT", default="3306")),
        "user": _env("MYSQL_USER", "DB_USERNAME"),
        "password": _env("MYSQL_PASSWORD", "DB_PASSWORD"),
        "database": _env("MYSQL_DATABASE", "DB_NAME"),
    }
    if not all([cfg["user"], cfg["password"], cfg["database"]]):
        raise ValueError(
            "缺少数据库配置：需要 MYSQL_USER/PASSWORD/DATABASE（或 DB_USERNAME/DB_PASSWORD/DB_NAME）"
        )
    return cfg


def _connect():
    cfg = get_db_config()
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
    )


@mcp.tool()
async def execute_sql(query: str) -> str:
    """执行 SQL 查询并返回结果。

    Args:
        query: SQL 语句。SELECT/SHOW 返回 JSON 行数组；其余返回受影响行数。
    """
    logger.info("execute_sql: %s", query[:300])
    try:
        conn = _connect()
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"数据库连接失败: {e}"}, ensure_ascii=False)
    try:
        with conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                head = query.strip().upper()
                if head.startswith("SELECT") or head.startswith("SHOW") or head.startswith("DESCRIBE"):
                    rows = cursor.fetchall()
                    return json.dumps(
                        {"rowCount": len(rows), "rows": rows},
                        ensure_ascii=False,
                        default=str,
                    )
                conn.commit()
                return json.dumps(
                    {"affectedRows": cursor.rowcount}, ensure_ascii=False
                )
    except Exception as e:  # noqa: BLE001
        logger.error("execute_sql 失败: %s", e)
        return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    logger.info("启动 MySQL MCP 服务器 (stdio) ...")
    mcp.run(transport="stdio")
