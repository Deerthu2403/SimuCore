"""Persistence layer (Phase 2).

Planned contents:
  * ``session.py``   - async SQLAlchemy engine/session factory from ``settings.database_url``
  * ``models.py``    - ORM models mirroring ``database/schema/001_init.sql``
  * ``repositories`` - TopologyRepo, MetricWriter (batched), PredictionRepo, AuditRepo
Alembic migrations live in ``backend/migrations``.
"""
