from __future__ import annotations

from . import tool


def _sched(rt):
    s = getattr(rt, "scheduler", None)
    if s is None:
        raise RuntimeError("automations run while the LocalAgent app (or `localagent daemon`) is open — "
                           "they aren't available in this window")
    return s


@tool("schedule_task", "Set up something to happen later or repeatedly, e.g. when='daily 08:00' "
      "task='summarize my unread email'. Schedules: 'every 2 hours', 'weekdays 9am', 'every monday 9:30', "
      "'in 20 minutes', 'once 2026-11-02 15:00'. notify='always' or 'only_if_important'.",
      {"task": {"type": "string", "description": "what to do, written as a full instruction"},
       "when": {"type": "string"}, "name": {"type": "string"},
       "notify": {"type": "string", "enum": ["always", "only_if_important"]}}, ["task", "when"], group="automations")
def schedule_task(rt, task: str, when: str, name: str = "", notify: str = "always") -> str:
    s = _sched(rt)
    if notify == "only_if_important":
        task += ("\n\nIf there is nothing worth telling the user, reply with exactly NOTHING_TO_REPORT.")
    it = s.add(task, when, name=name, notify="always" if notify != "only_if_important" else "important", created_by="agent")
    return f"Scheduled '{it['name']}' — {it['schedule']}. Next run: {it['next_run']}. The user can pause it on the Automations page."


@tool("list_automations", "Show scheduled tasks.", {}, group="automations")
def list_automations(rt) -> str:
    items = _sched(rt).list()
    if not items:
        return "no automations"
    return "\n".join(f"{a['id']}  {'on ' if a['enabled'] else 'off'}  {a['schedule']}  — {a['name']}  (next: {a.get('next_run') or '-'})"
                     for a in items)


@tool("pause_automation", "Turn a scheduled task off (or back on with enabled=true).",
      {"id": {"type": "string"}, "enabled": {"type": "boolean"}}, ["id"], group="automations")
def pause_automation(rt, id: str, enabled: bool = False) -> str:
    it = _sched(rt).update(id, enabled=bool(enabled))
    return f"{it['name']} is now {'on' if it['enabled'] else 'paused'}"
