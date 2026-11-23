"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers.batch_tables import router as batch_tables_router
from app.routers.resource import router as resource_router

ROUTERS = [batch_tables_router, resource_router]
