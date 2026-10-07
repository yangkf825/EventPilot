"""Executable researcher-owned workspace, never a pretend third-party backend.

Saving and accepting messages mutate a durable workspace file. Form drafts are
captured from live controls and restored through actual browser fill/select
operations. These capabilities are disclosed in every navigation prompt.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path


class OnlineWorkbench:
    def __init__(self, run_dir, goal, events, initial=None):
        self.path = Path(run_dir) / "workbench.json"
        self.events = {e.get("id", "E1"): e for e in events}
        self.state = {"goal": goal, "goal_version": 0, "goal_updates": [],
                      "acks": {}, "results": {}, "drafts": {},
                      "cancelled": False, "task_stopped": False}
        self.state.update(copy.deepcopy(initial or {}))
        self.state.setdefault("draft", {"text": goal, "sources": []})
        self.state.setdefault("draft_saved", False)
        self.state.setdefault("instruction_receipts", [])
        self.state.setdefault("packet_status", {})
        self.state.setdefault("exports", {})
        self.state.setdefault("draft_lost", False)
        self.persist()

    def persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def snapshot(self):
        return copy.deepcopy(self.state)

    async def execute(self, action, browser, phase):
        op = str(action.get("op", "")).upper()
        eid = action.get("event_id", phase if phase in self.events else None)
        if op in ("ACCEPT_EVENT", "ACK_EVENT"):
            if eid not in self.events:
                raise ValueError("Acknowledge a listed event_id")
            event = self.events[eid]
            execution = event.get("execution", {})
            kind = execution.get("kind", execution.get("type", ""))
            if op == "ACCEPT_EVENT" and kind in ("update_goal", "goal_update", "replan"):
                updated = execution.get("updated_goal", execution.get("new_goal"))
                if not isinstance(updated, str) or not updated.strip():
                    raise ValueError("Authorized update has no executable updated_goal")
                self.state["goal_version"] += 1
                self.state["goal"] = updated
                self.state["goal_updates"].append({"event_id": eid, "goal": updated,
                                                   "version": self.state["goal_version"]})
            if op == "ACCEPT_EVENT" and kind in ("cancel", "cancel_goal", "terminate"):
                self.state["cancelled"] = True
            self.state["acks"][eid] = True
            packet_id = execution.get("packet_id", action.get("packet_id"))
            if packet_id:
                self.state["packet_status"][packet_id] = "applied"
                for receipt in self.state["instruction_receipts"]:
                    if receipt.get("packet_id") == packet_id:
                        receipt["status"] = "applied"
                self.state["instruction_receipts"].append({"packet_id":packet_id,"kind":"receipt_status","status":"applied","sequence":len(self.state["instruction_receipts"])+1})
        elif op in ("STOP_TASK", "CANCEL_TASK"):
            # An explicit action is retained, so choosing TERMINATE alone is not
            # silently converted into a successful cancellation execution.
            self.state["task_stopped"] = True
        elif op == "SAVE_RESULT":
            text = action.get("text", action.get("answer"))
            sources = action.get("sources", [])
            if not isinstance(text, str) or not text.strip() or not isinstance(sources, list):
                raise ValueError("SAVE_RESULT needs nonempty text and a sources list")
            self.state["results"][phase] = {"text": text, "sources": sources,
                                              "goal": self.state["goal"],
                                              "goal_version": self.state["goal_version"],
                                              "observed_url": browser.page.url}
        elif op == "EXPORT_DRAFT":
            if not self.state.get("draft_saved") or self.state.get("draft_lost"):
                raise ValueError("Export requires a committed, retained draft")
            if self.state.get("rotation_started"):
                raise ValueError("Draft export window ended on the next task action")
            name = str(eid or action.get("name", "current"))
            packet = copy.deepcopy(self.state.get("committed_draft", self.state["draft"]))
            target = self.path.parent / ("export_"+name+".json")
            target.write_text(json.dumps(packet,ensure_ascii=False,indent=2),encoding="utf-8")
            self.state["exports"][name] = {"path":str(target),"packet":packet}
        elif op in ("SAVE_DRAFT", "COMMIT_DRAFT"):
            if self.state.get("draft_lost"):
                raise ValueError("The unsaved research draft has already been lost")
            self.state["draft_saved"] = True
            self.state["committed_draft"] = copy.deepcopy(self.state["draft"])
            if op == "COMMIT_DRAFT":
                self.persist()
                return self.snapshot()
            name = str(action.get("name", "current"))
            fields = await browser.page.evaluate("""() => Array.from(document.querySelectorAll('input,select,textarea'))
                .filter(e => e.getClientRects().length && !e.closest('[data-eventarena-panel]'))
                .map((e,index) => ({index,tag:e.tagName.toLowerCase(),name:e.name,id:e.id,
                  type:e.type,value:e.value,checked:e.checked,selected:Array.from(e.selectedOptions||[]).map(o=>o.value)}))""")
            self.state["drafts"][name] = {"url": browser.page.url, "fields": fields}
        elif op == "RESTORE_DRAFT":
            name = str(action.get("name", "current"))
            draft = self.state["drafts"].get(name)
            if not draft:
                raise ValueError("No saved draft with that name")
            if browser.page.url != draft["url"]:
                raise ValueError("Restore requires the same page URL as the saved draft")
            fields = browser.page.locator("input,select,textarea")
            for field in draft["fields"]:
                if field.get("id"):
                    selector = "#" + await browser.page.evaluate("s => CSS.escape(s)", field["id"])
                    locator = browser.page.locator(selector)
                elif field.get("name"):
                    escaped = json.dumps(field["name"])
                    locator = browser.page.locator(f"{field['tag']}[name={escaped}]")
                else:
                    locator = fields.nth(field["index"])
                if await locator.count() != 1:
                    raise ValueError("Saved field no longer uniquely identifies a control")
                if field["tag"] == "select":
                    await locator.select_option(field["selected"] or field["value"])
                elif field.get("type") in ("checkbox", "radio"):
                    await locator.set_checked(bool(field["checked"]))
                elif field.get("type") not in ("button", "submit", "hidden", "file", "reset"):
                    await locator.fill(field["value"])
        else:
            raise ValueError(f"Unsupported workbench operation {op}")
        self.persist()
        return self.snapshot()

    def before_task_action(self, events):
        """An explicitly declared workspace transition has an actual consequence."""
        loses = any(e.get("execution", {}).get("lose_unsaved_on_task_action") is True for e in events)
        if loses and not self.state.get("draft_saved"):
            self.state["draft_lost"] = True
            self.state["draft"] = {"text": "", "sources": []}
            self.persist()
        pending = [e.get("execution",{}).get("packet_id") for e in events if e.get("execution",{}).get("requires_before_task")]
        if any(p and self.state["packet_status"].get(p) != "applied" for p in pending):
            self.state["receipt_deadline_missed"] = True
        if any(e.get("execution",{}).get("operation") == "export_draft" and e.get("id") not in self.state["exports"] for e in events):
            self.state["export_deadline_missed"] = True
        self.state["rotation_started"] = True
        self.persist()


WORKBENCH_OPS = {"ACCEPT_EVENT", "ACK_EVENT", "SAVE_RESULT", "SAVE_DRAFT", "COMMIT_DRAFT", "RESTORE_DRAFT", "EXPORT_DRAFT", "STOP_TASK", "CANCEL_TASK"}
