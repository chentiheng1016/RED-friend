"""Skills plugin loader.

The
reload_skills tool stays in agent.py because it mutates agent.py's
module-level tools_list and _chat_state. This module hosts:

  - _SKILLS_DIR constant
  - _loaded_skills_info (updated at module load + by reload_skills)
  - _load_skills_from_dir (pure: returns (tools, info))
  - list_skills (pretty-prints _loaded_skills_info)

agent.py's reload_skills calls _load_skills_from_dir, writes the
resulting info back into this module's _loaded_skills_info, and then
updates its own tools_list + _chat_state.
"""
import os
import sys

from agent_core.logging_and_paths import logger, _SCRIPT_DIR, startup_print

_SKILLS_DIR = os.path.join(_SCRIPT_DIR, "skills")
_loaded_skills_info = []  # populated at module load by agent.py; also by reload_skills


def _load_skills_from_dir(skills_dir: str):
    """掃描 skills_dir，回傳 (tools_list_to_append, info_list)。
    失敗的檔案會 log 警告跳過，不影響其他 skill。
    為了讓 reload 真的拿到檔案最新內容，每次載入前清 sys.modules 對應條目。"""
    import importlib.util

    tools = []
    info = []

    if not os.path.isdir(skills_dir):
        return tools, info

    for mod_name in list(sys.modules):
        if mod_name.startswith("xiaohong_skills"):
            del sys.modules[mod_name]

    for filename in sorted(os.listdir(skills_dir)):
        if not filename.endswith(".py"):
            continue
        if filename.startswith("_") or filename.startswith("."):
            continue

        filepath = os.path.join(skills_dir, filename)
        module_name = f"xiaohong_skills.{filename[:-3]}"

        try:
            spec = importlib.util.spec_from_file_location(module_name, filepath)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as e:
            logger.warning("skill 載入失敗 %s：%s", filename, e)
            sys.modules.pop(module_name, None)
            info.append({
                "file": filename, "tools": [], "error": str(e),
                "description": "",
            })
            continue

        skill_tools = getattr(module, "SKILL_TOOLS", None)
        if not isinstance(skill_tools, list):
            logger.warning("skill %s 沒有 SKILL_TOOLS list，跳過", filename)
            info.append({
                "file": filename, "tools": [], "error": "沒有 SKILL_TOOLS",
                "description": "",
            })
            continue

        # Dry-run registry 裡有 describe-fn 的 skill tool 也要包裝
        # （builtins 在 tool_registry_catalog.build_builtin_tools 做這件事）
        from agent_core.dry_run import (
            get_dry_run_describer as _get_dry_run_describer,
            respects_dry_run as _respects_dry_run,
        )

        names = []
        for fn in skill_tools:
            if not callable(fn):
                continue
            try:
                fn._is_skill = True
                fn._skill_file = filename
            except (AttributeError, TypeError):
                logger.warning("skill %s 內的 %s 無法 setattr，可能是 built-in，跳過",
                               filename, getattr(fn, "__name__", "?"))
                continue
            # 包 audit（對齊 build_builtin_tools：audit 在內、dry-run 在外）。
            # skill 工具繞過 build_builtin_tools，不在這補套的話，列在 _AUDITED_TOOLS
            # 的 skill 工具（如 send_briefing_email / push_briefing_telegram）會完全
            # 沒有 run_history 稽查軌跡。
            from agent_core.tool_registry_catalog import _AUDITED_TOOLS as _AUD
            from agent_core.run_history import audited as _audited
            if fn.__name__ in _AUD:
                fn = _audited(**_AUD[fn.__name__])(fn)
            # 包 dry-run（自訂描述或 sensitive policy 命中）
            describe = _get_dry_run_describer(fn.__name__)
            if describe and not getattr(fn, "_dry_run_wrapped", False):
                fn = _respects_dry_run(describe=describe)(fn)
            # wrap 完成後再確保自訂屬性落在最終 wrapper 上（functools.wraps 會複製
            # __dict__、實測已保留，這是 belt-and-suspenders 回應 review 疑慮）。
            try:
                fn._is_skill = True
                fn._skill_file = filename
            except (AttributeError, TypeError):
                pass
            tools.append(fn)
            names.append(getattr(fn, "__name__", "?"))

        info.append({
            "file": filename,
            "tools": names,
            "error": None,
            "description": (getattr(module, "__doc__", "") or "").strip()[:200],
        })
        startup_print(f"[skills] ✅ 載入 {filename}（{len(names)} 個工具：{', '.join(names)}）")

    return tools, info


def list_skills():
    """列出 skills/ 資料夾目前載入的所有 skill 與其工具。"""
    if not _loaded_skills_info:
        return "目前沒有載入任何 skill。放 .py 檔到 skills/ 資料夾即可。"
    lines = [f"共 {len(_loaded_skills_info)} 個 skill："]
    for s in _loaded_skills_info:
        if s.get("error"):
            lines.append(f"  ❌ {s['file']} — 載入失敗：{s['error']}")
            continue
        tool_str = ", ".join(s["tools"]) if s["tools"] else "(無工具)"
        desc = f"\n      {s['description']}" if s.get("description") else ""
        lines.append(f"  ✅ {s['file']} → [{tool_str}]{desc}")
    return "\n".join(lines)
