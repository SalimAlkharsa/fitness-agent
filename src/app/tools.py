"""Agent tools + registry types.

A Tool bundles a name, a Pydantic args model (whose JSON schema is shown to the
model and which validates its calls), a handler, and a `confirm_required` flag.
Confirm-gated tools never execute inside the loop — the agent returns a pending
payload the web layer renders as a card, and the write happens only on approval.

The read/analysis surface is a small, fixed menu of typed, deterministic tools —
`query_history`, `describe_data`, `exercise_stats`, `personal_records`,
`make_chart` — so the internet-exposed agent never authors or runs code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from app.models import ParsedWorkout, ParseResult

if TYPE_CHECKING:
    from app.models import KBEntry
    from app.store import SheetsStore


@dataclass
class ToolContext:
    """Runtime dependencies handed to a tool handler."""

    store: SheetsStore | None
    kb: list[KBEntry]
    user_text: str


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Callable[[BaseModel, ToolContext], object]
    confirm_required: bool = False
    summarize: Callable[[BaseModel], str] | None = None

    def json_schema(self) -> dict:
        return self.args_model.model_json_schema()


# --- log_workout (confirm-gated) ---


def summarize_parsed_workout(pw: ParsedWorkout) -> str:
    """One-line human preview for the confirm card / reply."""
    head = f"{pw.type} on {pw.date}"
    if pw.start and pw.end:
        head += f" ({pw.start}–{pw.end})"
    if not pw.sets:
        return head
    parts = []
    for sg in pw.sets:
        load = ""
        if sg.total_lbs is not None:
            load = f" @ {sg.total_lbs:g}"
        elif sg.assist_lbs is not None:
            load = f" (assist {sg.assist_lbs:g})"
        parts.append(f"{sg.exercise} {sg.sets}x{sg.reps:g}{load}")
    return head + ": " + "; ".join(parts)


def summarize_parse_result(pr: ParseResult) -> str:
    return " | ".join(summarize_parsed_workout(w) for w in pr.workouts) or "(nothing)"


def _log_workout_handler(args: BaseModel, ctx: ToolContext) -> object:
    # Unreachable in the loop: confirm_required short-circuits before execution.
    # The append happens on the user's ✅ (app/logflow.py + the web layer).
    raise RuntimeError("log_workout is confirm-gated; commit happens on confirm")


LOG_WORKOUT = Tool(
    name="log_workout",
    description=(
        "Record the workout(s) parsed from the user's message. Provide a "
        "ParseResult: a `workouts` list, one entry per session (a lifting session "
        "and a basketball game are two entries). Confirm-gated: the user reviews an "
        "editable card per workout before anything is written."
    ),
    args_model=ParseResult,
    handler=_log_workout_handler,
    confirm_required=True,
    summarize=lambda pr: summarize_parse_result(pr),  # type: ignore[arg-type]
)


# --- undo (confirm-gated) ---


class UndoArgs(BaseModel):
    workout_id: str | None = Field(
        default=None,
        description="the workout to delete; omit to target the most recent one",
    )


def _undo_handler(args: BaseModel, ctx: ToolContext) -> object:
    raise RuntimeError("undo is confirm-gated; deletion happens on confirm")


UNDO = Tool(
    name="undo",
    description=(
        "Delete a logged workout and all its sets. Omit workout_id to remove the "
        "most recent one. Confirm-gated: the user approves the deletion first."
    ),
    args_model=UndoArgs,
    handler=_undo_handler,
    confirm_required=True,
    summarize=lambda a: "Delete a workout",  # web layer resolves the real target
)


# --- edit_workout (confirm-gated) ---


class EditWorkoutArgs(BaseModel):
    workout_id: str | None = Field(
        default=None,
        description="the workout to edit; omit to target the most recent one. "
        "Look it up with query_history first if it isn't the most recent.",
    )


def _edit_workout_handler(args: BaseModel, ctx: ToolContext) -> object:
    raise RuntimeError("edit_workout is confirm-gated; the change happens on confirm")


EDIT_WORKOUT = Tool(
    name="edit_workout",
    description=(
        "Correct a previously logged workout's weight, reps, sets, or date. "
        "Omit workout_id to target the most recent one. Confirm-gated: opens the "
        "same editable card as logging, pre-filled with the workout's current "
        "values, so the user corrects the exact field rather than you re-guessing it. "
        "Renaming the exercise itself isn't reliable here — use log_workout + undo "
        "for that instead."
    ),
    args_model=EditWorkoutArgs,
    handler=_edit_workout_handler,
    confirm_required=True,
    summarize=lambda a: "Edit a workout",
)


# --- query_history (read) ---


DEFAULT_QUERY_LIMIT = 1000


class QueryHistoryArgs(BaseModel):
    exercise: str | None = Field(
        default=None, description="canonical or alias exercise name; omit for all"
    )
    date_from: str | None = Field(default=None, description="YYYY-MM-DD inclusive")
    date_to: str | None = Field(default=None, description="YYYY-MM-DD inclusive")
    type: str | None = Field(default=None, description="session type, e.g. basketball")
    limit: int = Field(
        default=DEFAULT_QUERY_LIMIT,
        ge=1,
        le=DEFAULT_QUERY_LIMIT,
        description="max rows/sessions to return (capped); narrow the filters for more",
    )


def _query_history_handler(args: BaseModel, ctx: ToolContext) -> object:
    assert isinstance(args, QueryHistoryArgs)
    if ctx.store is None:
        return {"rows": [], "count": 0}
    from app.prompt import resolve_exercise

    exercise = resolve_exercise(args.exercise, ctx.kb) or args.exercise if args.exercise else None
    all_workouts = ctx.store.read_workouts()
    workouts = {w.workout_id: w for w in all_workouts}
    sets = ctx.store.read_sets(exercise=exercise, date_from=args.date_from, date_to=args.date_to)
    rows = []
    for s in sets:
        w = workouts.get(s.workout_id)
        if args.type and (w is None or w.type.lower() != args.type.lower()):
            continue
        row = s.model_dump()
        row["date"] = w.date if w else None
        row["type"] = w.type if w else None
        rows.append(row)
    rows.sort(key=lambda r: (r.get("date") or "", r["workout_id"]))

    # Sessions matching the filters — includes sport sessions that have no set rows
    # (e.g. basketball), so "how many times did I play X" counts correctly.
    def _in_range(d: str) -> bool:
        if args.date_from and d < args.date_from:
            return False
        return not (args.date_to and d > args.date_to)

    sessions = [
        {"workout_id": w.workout_id, "date": w.date, "type": w.type}
        for w in all_workouts
        if (not args.type or w.type.lower() == args.type.lower()) and _in_range(w.date)
    ]
    # Cap by rows/sessions, never by slicing characters — that would feed the model
    # truncated, invalid JSON. Counts stay honest and `truncated` flags any drop.
    n_rows, n_sessions = len(rows), len(sessions)
    truncated = n_rows > args.limit or n_sessions > args.limit
    out = {
        "rows": rows[: args.limit],
        "count": n_rows,
        "sessions": sessions[: args.limit],
        "session_count": n_sessions,
    }
    if truncated:
        out["truncated"] = True
        out["note"] = f"showing the first {args.limit}; narrow the date range or exercise for more"
    return out


QUERY_HISTORY = Tool(
    name="query_history",
    description=(
        "Read logged set-groups joined to their workout date + type, optionally "
        "filtered by exercise (alias-aware), date range, or session type. Returns "
        "rows as JSON sorted by date. Runs without confirm."
    ),
    args_model=QueryHistoryArgs,
    handler=_query_history_handler,
    confirm_required=False,
)


# --- describe_data / exercise_stats / personal_records (read analysis) ---
#
# Every handler here passes only typed params into analysis.py (comparisons and
# KB lookups) — never a model-authored string into anything eval-like. See
# analysis.py's injection-safe boundary note.


class DescribeDataArgs(BaseModel):
    since: str | None = Field(
        default=None, description="YYYY-MM-DD inclusive lower bound; omit for all time"
    )


def _describe_data_handler(args: BaseModel, ctx: ToolContext) -> object:
    assert isinstance(args, DescribeDataArgs)
    if ctx.store is None:
        return {"n_workouts": 0, "n_sets": 0, "exercises": [], "types": [], "last_workout": None}
    from app import analysis

    return analysis.describe(ctx.store, since=args.since)


DESCRIBE_DATA = Tool(
    name="describe_data",
    description=(
        "Overview of the logged data so you can orient before querying: workout/set "
        "counts, date range, the most-trained exercises (with session counts + last "
        "date), session types, and the latest workout. Read-only."
    ),
    args_model=DescribeDataArgs,
    handler=_describe_data_handler,
    confirm_required=False,
)


class ExerciseStatsArgs(BaseModel):
    exercise: str = Field(description="exercise to analyze (canonical or alias)")
    metric: Literal["volume", "top_set", "pr", "frequency"] = Field(
        default="volume",
        description="per-date series: volume (sets·reps·total), top_set/pr (heaviest "
        "total per date), or frequency (sessions per month)",
    )
    date_from: str | None = Field(default=None, description="YYYY-MM-DD inclusive")
    date_to: str | None = Field(default=None, description="YYYY-MM-DD inclusive")


def _exercise_stats_handler(args: BaseModel, ctx: ToolContext) -> object:
    assert isinstance(args, ExerciseStatsArgs)
    if ctx.store is None:
        return {"exercise": args.exercise, "n_sessions": 0, "series": [], "pr": None}
    from app import analysis

    df = analysis.load_joined(ctx.store)
    return analysis.exercise_stats(
        df, args.exercise, ctx.kb, args.metric, args.date_from, args.date_to
    )


EXERCISE_STATS = Tool(
    name="exercise_stats",
    description=(
        "Per-exercise analytics for ONE exercise: a per-date series (volume, top_set, "
        "pr, or frequency), the all-time PR, and a first→last trend delta. Use for "
        "progress / 'am I improving on X' questions. Read-only."
    ),
    args_model=ExerciseStatsArgs,
    handler=_exercise_stats_handler,
    confirm_required=False,
)


class PersonalRecordsArgs(BaseModel):
    exercise: str | None = Field(
        default=None, description="one exercise (canonical/alias); omit for ALL tracked"
    )
    reps: float | None = Field(
        default=None, description="rep-specific PR (e.g. best 5-rep set); omit for any reps"
    )


def _personal_records_handler(args: BaseModel, ctx: ToolContext) -> object:
    assert isinstance(args, PersonalRecordsArgs)
    if ctx.store is None:
        return {"records": []}
    from app import analysis

    df = analysis.load_joined(ctx.store)
    return {"records": analysis.all_personal_records(df, ctx.kb, args.exercise, args.reps)}


PERSONAL_RECORDS = Tool(
    name="personal_records",
    description=(
        "Personal records (max total_lbs + the rep count) across ALL tracked "
        "exercises in one call, or one exercise if given, heaviest-first. Optional "
        "`reps` for a rep-specific PR. Use for 'what are my PRs'. Read-only."
    ),
    args_model=PersonalRecordsArgs,
    handler=_personal_records_handler,
    confirm_required=False,
)


# --- make_chart (read) ---


class ChartSpec(BaseModel):
    exercise: str = Field(description="exercise to chart (alias-aware)")
    metric: str = Field(default="volume", description="'volume' or 'top_set'")
    title: str | None = Field(default=None, description="optional chart title")


def _make_chart_handler(args: BaseModel, ctx: ToolContext) -> object:
    assert isinstance(args, ChartSpec)
    if ctx.store is None:
        return {"error": "no data store configured"}
    from app import charts

    return charts.render_exercise_chart(
        ctx.store, args.exercise, metric=args.metric, title=args.title, kb=ctx.kb
    )


MAKE_CHART = Tool(
    name="make_chart",
    description=(
        "Render a line chart for one exercise over time (metric 'volume' or "
        "'top_set'). The chart is shown inline in the chat automatically — just "
        "add one sentence interpreting the trend in your final answer."
    ),
    args_model=ChartSpec,
    handler=_make_chart_handler,
    confirm_required=False,
)


# --- read_skill (read) ---


class ReadSkillArgs(BaseModel):
    name: str = Field(description="skill name from the index, e.g. parsing-salims-logs")


def _read_skill_handler(args: BaseModel, ctx: ToolContext) -> object:
    assert isinstance(args, ReadSkillArgs)
    from app.skills import read_skill

    text = read_skill(args.name)
    if text is None:
        return {"error": f"no skill named '{args.name}'"}
    return {"skill": args.name, "content": text}


READ_SKILL = Tool(
    name="read_skill",
    description=(
        "Read the full text of a skill (guidance doc) by name. Read the parsing "
        "skill before logging a workout; read analysis/charting before answering "
        "questions or making charts."
    ),
    args_model=ReadSkillArgs,
    handler=_read_skill_handler,
    confirm_required=False,
)


def default_tools() -> list[Tool]:
    return [
        READ_SKILL,
        LOG_WORKOUT,
        QUERY_HISTORY,
        DESCRIBE_DATA,
        EXERCISE_STATS,
        PERSONAL_RECORDS,
        MAKE_CHART,
        UNDO,
        EDIT_WORKOUT,
    ]
