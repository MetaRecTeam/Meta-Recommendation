"""WP8 graph-trace store: traces, their events and their graph instances.

Creates three empty tables. Nothing writes to them until the trace API lands
and DEBUG_GRAPH_TRACE_ENABLED is on, so applying this migration changes no
existing behaviour.

Layout follows the page contract. One row per trace holds the config and
topology snapshot the page echoes back; one row per event keyed
``(trace_id, sequence)``, which is both the primary key and the cursor index,
so a page read is a range scan rather than a sort; one row per graph instance.
``next_sequence`` on the trace row is what makes sequence allocation and
append a single transaction (DECISIONS.md R7): a writer takes ``FOR UPDATE``
on the trace, reads the number, inserts the event and bumps it together.

Events cascade from the trace, so deleting an expired trace takes its events
and instances with it in one statement.

Revision ID: 20261115_0008
Revises: 20260821_0007
Create Date: 2026-11-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "20261115_0008"
down_revision = "20260821_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wp8_traces",
        sa.Column("trace_id", sa.String(length=128), primary_key=True),
        sa.Column("debug_run_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default=sa.text("'running'")),
        # The applied config and the static topology, echoed on every page.
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("definitions", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("redaction_policy_id", sa.String(length=128), nullable=False),
        # Allocation counter; see the module docstring (R7).
        sa.Column("next_sequence", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("dropped_event_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("redacted_value_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("truncated_value_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed', 'cancelled', 'incomplete')",
            name="ck_wp8_traces_status",
        ),
        sa.CheckConstraint("expires_at > created_at", name="ck_wp8_traces_expiry_after_creation"),
        sa.CheckConstraint("next_sequence >= 1", name="ck_wp8_traces_next_sequence"),
        sa.CheckConstraint("dropped_event_count >= 0", name="ck_wp8_traces_dropped_count"),
    )
    # The API is keyed by debug run, not by trace id. One trace per run.
    op.create_index("ix_wp8_traces_debug_run", "wp8_traces", ["debug_run_id"], unique=True)
    # Retention sweep: "everything past its expiry", cheapest as an index scan.
    op.create_index("ix_wp8_traces_expires_at", "wp8_traces", ["expires_at"])
    # Start-up recovery: traces left running by a process that died (R8).
    op.create_index(
        "ix_wp8_traces_running",
        "wp8_traces",
        ["status"],
        postgresql_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "wp8_trace_events",
        sa.Column(
            "trace_id",
            sa.String(length=128),
            sa.ForeignKey("wp8_traces.trace_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Primary key with trace_id, so a page read after a cursor is a range
        # scan on the primary key and never a sort.
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("graph_instance_id", sa.String(length=128), nullable=True),
        sa.Column("node_id", sa.String(length=128), nullable=True),
        sa.Column("timestamp_utc", sa.DateTime(timezone=True), nullable=False),
        # The whole contract event. Stored once, served as-is: a page must not
        # be a different object from what passed validation on the way in.
        sa.Column("event", postgresql.JSONB(), nullable=False),
        sa.Column("stored_bytes", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.CheckConstraint("sequence >= 1", name="ck_wp8_trace_events_sequence"),
    )
    op.create_index(
        "ix_wp8_trace_events_event_id", "wp8_trace_events", ["trace_id", "event_id"], unique=True
    )

    op.create_table(
        "wp8_trace_instances",
        sa.Column(
            "trace_id",
            sa.String(length=128),
            sa.ForeignKey("wp8_traces.trace_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("graph_instance_id", sa.String(length=128), primary_key=True),
        sa.Column("graph_definition_id", sa.String(length=128), nullable=False),
        sa.Column("started_sequence", sa.Integer(), nullable=False),
        sa.Column("ended_sequence", sa.Integer(), nullable=True),
        sa.Column("instance", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("started_sequence >= 1", name="ck_wp8_trace_instances_started"),
    )


def downgrade() -> None:
    op.drop_table("wp8_trace_instances")
    op.drop_index("ix_wp8_trace_events_event_id", table_name="wp8_trace_events")
    op.drop_table("wp8_trace_events")
    op.drop_index("ix_wp8_traces_running", table_name="wp8_traces")
    op.drop_index("ix_wp8_traces_expires_at", table_name="wp8_traces")
    op.drop_index("ix_wp8_traces_debug_run", table_name="wp8_traces")
    op.drop_table("wp8_traces")
