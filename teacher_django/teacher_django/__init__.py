from .celery import app as celery_app
from .celery import app as celery

__all__ = ('celery_app', 'celery')

# 用 PyMySQL 冒充 mysqlclient(MySQLdb)。
# 原因：conda 版 mysqlclient 的 C 扩展在与 MySQL 8（caching_sha2_password 认证插件）
# 握手时会段错误崩溃；PyMySQL 是纯 Python 实现，稳定且能正确处理该插件。
# Django 5.x 要求 MySQLdb 版本 >= 1.4.3，PyMySQL 自报 1.2.0 会被拒，故先伪装版本号。
# SQLite 模式下这段只是多导入一个模块，无副作用。
import pymysql

pymysql.version_info = (1, 4, 6, "final", 0)
pymysql.install_as_MySQLdb()
