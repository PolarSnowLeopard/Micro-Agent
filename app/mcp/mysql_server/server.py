import os
import logging
from mysql.connector import connect, Error
from mcp.server.fastmcp import FastMCP
from typing import Dict, List, Any, Optional

from dotenv import load_dotenv

load_dotenv()

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("mysql_mcp_server")

# 创建 MCP 服务器
mcp = FastMCP("mysql_mcp_server")

def get_db_config():
    """从环境变量获取数据库配置"""
    config = {
        "host": os.getenv("MYSQL_HOST", "localhost"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE")
    }
    
    if not all([config["user"], config["password"], config["database"]]):
        logger.error("缺少必要的数据库配置。请检查环境变量：")
        logger.error("MYSQL_USER、MYSQL_PASSWORD 和 MYSQL_DATABASE 是必需的")
        raise ValueError("缺少必要的数据库配置")
    
    return config

# 列出所有表作为资源
@mcp.resource("mysql://tables")
async def list_tables() -> str:
    """列出MySQL中的所有表"""
    config = get_db_config()
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SHOW TABLES")
                tables = cursor.fetchall()
                logger.info(f"找到表：{tables}")
                
                result = [f"数据库 {config['database']} 中的表:"]
                for table in tables:
                    result.append(f"- {table[0]}")
                return "\n".join(result)
    except Error as e:
        logger.error(f"列出表失败：{str(e)}")
        return f"列出表时出错：{str(e)}"

# 按表名提供资源的模式
@mcp.resource("mysql://{table_name}/data")
async def get_table_data(table_name: str) -> str:
    """获取表的内容"""
    config = get_db_config()
    logger.info(f"读取表：{table_name}")
    
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"SELECT * FROM {table_name} LIMIT 100")
                columns = [desc[0] for desc in cursor.description]
                rows = cursor.fetchall()
                result = [",".join(map(str, row)) for row in rows]
                return "\n".join([",".join(columns)] + result)
                
    except Error as e:
        logger.error(f"读取表 {table_name} 时发生数据库错误：{str(e)}")
        raise RuntimeError(f"数据库错误：{str(e)}")

@mcp.resource("mysql://{table_name}/schema")
async def get_table_schema(table_name: str) -> str:
    """获取表的结构"""
    config = get_db_config()
    logger.info(f"读取表结构：{table_name}")
    
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"DESCRIBE {table_name}")
                columns = cursor.fetchall()
                result = ["字段名称,类型,允许空值,键,默认值,额外信息"]
                for col in columns:
                    result.append(",".join(str(c) if c is not None else "NULL" for c in col))
                return "\n".join(result)
                
    except Error as e:
        logger.error(f"读取表结构 {table_name} 时发生数据库错误：{str(e)}")
        raise RuntimeError(f"数据库错误：{str(e)}")

@mcp.tool()
async def execute_sql(query: str) -> Dict[str, Any]:
    """执行SQL查询
    
    Args:
        query: 要执行的SQL查询
    """
    config = get_db_config()
    logger.info(f"执行SQL查询：{query}")
    
    try:
        with connect(**config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(query)
                
                # 特殊处理 SHOW TABLES
                if query.strip().upper().startswith("SHOW TABLES"):
                    tables = cursor.fetchall()
                    result = ["Tables_in_" + config["database"]]  # 表头
                    result.extend([table[0] for table in tables])
                    return {
                        "content": [{
                            "type": "text",
                            "text": "\n".join(result)
                        }]
                    }
                
                # 常规 SELECT 查询
                elif query.strip().upper().startswith("SELECT"):
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    result = [",".join(map(str, row)) for row in rows]
                    return {
                        "content": [{
                            "type": "text", 
                            "text": "\n".join([",".join(columns)] + result)
                        }]
                    }
                
                # 非 SELECT 查询
                else:
                    conn.commit()
                    return {
                        "content": [{
                            "type": "text",
                            "text": f"查询执行成功。影响的行数：{cursor.rowcount}"
                        }]
                    }
                
    except Error as e:
        logger.error(f"执行SQL '{query}' 时发生错误：{e}")
        return {
            "content": [{
                "type": "text",
                "text": f"执行查询时出错：{str(e)}"
            }],
            "isError": True
        }

if __name__ == "__main__":
    # 使用stdio作为传输层运行服务器
    logger.info("启动MySQL MCP服务器...")
    
    try:
        config = get_db_config()
        logger.info(f"数据库配置：{config['host']}/{config['database']} 用户：{config['user']}")
        mcp.run(transport='stdio')
    except Exception as e:
        logger.error(f"服务器错误：{str(e)}", exc_info=True)
        raise 