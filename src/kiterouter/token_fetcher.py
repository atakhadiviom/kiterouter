"""Automated token and credential extraction from local applications, IDEs, and OmniRoute."""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("kiterouter.token_fetcher")


class TokenFetcher:
    """Extracts tokens and credentials directly from local apps and existing stores."""

    @staticmethod
    def fetch_from_9router(target_provider: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        Extract active OAuth tokens and API keys directly from 9Router SQLite DB:
        ~/.9router/db/data.sqlite
        """
        results: Dict[str, Dict[str, Any]] = {}
        db_path = Path.home() / ".9router" / "db" / "data.sqlite"
        if not db_path.exists():
            return results

        try:
            # Read-only URI mode to prevent any locking
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            c = conn.cursor()
            c.execute(
                """
                SELECT provider, authType, email, data, isActive
                FROM providerConnections
                WHERE isActive = 1
                ORDER BY priority ASC
                """
            )
            rows = c.fetchall()
            conn.close()

            for row in rows:
                p_name, a_type, email, data_str, is_active = row
                p_clean = p_name.lower().replace("-", "_")

                # Normalize provider IDs
                if "openai_compatible" in p_clean or "custom" in p_clean:
                    p_clean = "custom"
                elif p_clean == "agy":
                    p_clean = "antigravity"
                elif p_clean == "github":
                    p_clean = "copilot"
                elif p_clean == "kilocode":
                    p_clean = "kiro"

                if target_provider and p_clean != target_provider:
                    continue

                creds_data: Dict[str, Any] = {}
                if data_str:
                    try:
                        creds_data = json.loads(data_str)
                    except Exception:
                        pass

                if p_clean not in results:
                    creds: Dict[str, Any] = {"enabled": True, "source": "9router"}
                    
                    at = creds_data.get("accessToken")
                    rt = creds_data.get("refreshToken")
                    ak = creds_data.get("apiKey")
                    proj = creds_data.get("projectId")

                    if at:
                        creds["token"] = at
                        creds["access_token"] = at
                    if rt:
                        creds["refresh_token"] = rt
                    if ak:
                        creds["api_key"] = ak
                    if proj:
                        creds["project_id"] = proj
                    if email:
                        creds["email"] = email

                    spec = creds_data.get("providerSpecificData", {})
                    if isinstance(spec, dict):
                        if "machineId" in spec:
                            creds["machine_id"] = spec["machineId"]
                        if "baseUrl" in spec:
                            creds["base_url"] = spec["baseUrl"]
                        if "defaultModel" in creds_data and not creds.get("default_model"):
                            creds["default_model"] = creds_data["defaultModel"]

                    results[p_clean] = creds

        except Exception as e:
            logger.warning(f"Failed to read from 9Router sqlite: {e}")

        return results

    @staticmethod
    def fetch_from_omniroute(target_provider: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
        """
        Import active tokens stored in OmniRoute (~/.omniroute/storage.sqlite).
        """
        db_path = Path.home() / ".omniroute" / "storage.sqlite"
        if not db_path.exists():
            return {}

        results: Dict[str, Any] = {}
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            c = conn.cursor()
            query = """
                SELECT provider, auth_type, email, access_token, refresh_token, api_key, project_id, provider_specific_data
                FROM provider_connections
                WHERE is_active = 1
                ORDER BY updated_at DESC
            """
            c.execute(query)
            rows = c.fetchall()
            conn.close()

            for row in rows:
                p_name, a_type, email, at, rt, ak, proj, spec = row
                p_clean = p_name.lower().replace("-", "_")

                # Map alias names
                if p_clean == "agy":
                    p_clean = "antigravity"
                elif p_clean == "github":
                    p_clean = "copilot"
                elif p_clean in ("cmd", "commandcode"):
                    p_clean = "command_code"

                target_clean = target_provider.lower().replace("-", "_") if target_provider else None
                if target_clean in ("cmd", "commandcode"):
                    target_clean = "command_code"

                if target_clean and p_clean != target_clean:
                    continue

                if p_clean not in results:
                    creds: Dict[str, Any] = {"enabled": True, "source": "omniroute"}
                    if at:
                        creds["token"] = at
                        creds["access_token"] = at
                    if rt:
                        creds["refresh_token"] = rt
                    if ak:
                        creds["api_key"] = ak
                    if proj:
                        creds["project_id"] = proj
                    if email:
                        creds["email"] = email

                    if spec:
                        try:
                            extra = json.loads(spec)
                            if isinstance(extra, dict):
                                if "machineId" in extra:
                                    creds["machine_id"] = extra["machineId"]
                                if "projectId" in extra and not creds.get("project_id"):
                                    creds["project_id"] = extra["projectId"]
                        except Exception:
                            pass

                    results[p_clean] = creds

        except Exception as e:
            logger.warning(f"Failed to read from OmniRoute sqlite: {e}")

        return results

    @staticmethod
    def fetch_cursor_credentials() -> Dict[str, Any]:
        """
        Extract Cursor credentials and serviceMachineId from local Cursor state.vscdb or auth.json.
        """
        # 1. Check official cursor-agent auth.json
        agent_paths = [
            Path.home() / ".config" / "cursor" / "auth.json",
            Path.home() / ".cursor" / "agent-cli-state.json",
        ]
        for ap in agent_paths:
            if ap.exists():
                try:
                    with open(ap, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    token = data.get("accessToken") or data.get("token")
                    if token:
                        return {
                            "token": token,
                            "source": "cursor-agent",
                            "enabled": True,
                        }
                except Exception:
                    pass

        # 2. Check Cursor IDE SQLite state.vscdb
        vscdb_candidates = [
            Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb",
            Path.home() / "Library/Application Support/Cursor - Insiders/User/globalStorage/state.vscdb",
            Path.home() / ".config/Cursor/User/globalStorage/state.vscdb",
        ]
        for db in vscdb_candidates:
            if db.exists():
                try:
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                    c = conn.cursor()
                    keys = [
                        "cursorAuth/accessToken",
                        "cursorAuth/refreshToken",
                        "cursorAuth/cachedEmail",
                        "storage.serviceMachineId",
                    ]
                    placeholders = ",".join(["?"] * len(keys))
                    c.execute(f"SELECT key, value FROM itemTable WHERE key IN ({placeholders})", keys)
                    rows = dict(c.fetchall())
                    conn.close()

                    token = rows.get("cursorAuth/accessToken")
                    machine_id = rows.get("storage.serviceMachineId")
                    email = rows.get("cursorAuth/cachedEmail")

                    if token:
                        return {
                            "token": token,
                            "machine_id": machine_id or "",
                            "email": email or "",
                            "source": "cursor-ide",
                            "enabled": True,
                        }
                except Exception as e:
                    logger.warning(f"Error reading Cursor state.vscdb at {db}: {e}")

        # 3. Fallback to OmniRoute
        omni = TokenFetcher.fetch_from_omniroute("cursor")
        if "cursor" in omni:
            return omni["cursor"]

        return {}

    @staticmethod
    def fetch_antigravity_credentials() -> Dict[str, Any]:
        """
        Extract Antigravity / Google Cloud Code Assist token and project.
        """
        # 1. Antigravity IDE state.vscdb
        vscdb_candidates = [
            Path.home() / "Library/Application Support/Antigravity/User/globalStorage/state.vscdb",
            Path.home() / ".config/Antigravity/User/globalStorage/state.vscdb",
        ]
        for db in vscdb_candidates:
            if db.exists():
                try:
                    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
                    c = conn.cursor()
                    c.execute("SELECT value FROM itemTable WHERE key='antigravityAuthStatus'")
                    row = c.fetchone()
                    conn.close()
                    if row:
                        status_data = json.loads(row[0])
                        token = status_data.get("apiKey") or status_data.get("token") or status_data.get("accessToken")
                        email = status_data.get("email")
                        if token:
                            return {
                                "token": token,
                                "email": email or "",
                                "project_id": "aicode-consumers",
                                "source": "antigravity-ide",
                                "enabled": True,
                            }
                except Exception as e:
                    logger.warning(f"Error reading Antigravity DB at {db}: {e}")

        # 2. Check gcloud CLI
        gcloud_adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if gcloud_adc.exists():
            try:
                with open(gcloud_adc, "r", encoding="utf-8") as f:
                    data = json.load(f)
                token = data.get("refresh_token") or data.get("token")
                proj = data.get("quota_project_id") or "aicode-consumers"
                if token:
                    return {
                        "token": token,
                        "project_id": proj,
                        "source": "gcloud-adc",
                        "enabled": True,
                    }
            except Exception:
                pass

        # 3. Fallback to OmniRoute
        omni = TokenFetcher.fetch_from_omniroute("antigravity")
        if "antigravity" in omni:
            return omni["antigravity"]

        return {}

    @staticmethod
    def fetch_copilot_credentials() -> Dict[str, Any]:
        """
        Extract GitHub Copilot token via `gh auth token` or hosts.json.
        """
        # 1. Check gh CLI
        if shutil.which("gh"):
            try:
                out = subprocess.check_output(["gh", "auth", "token"], text=True, timeout=3).strip()
                if out:
                    return {
                        "token": out,
                        "source": "gh-cli",
                        "enabled": True,
                    }
            except Exception:
                pass

        # 2. Check ~/.config/github-copilot/hosts.json
        copilot_hosts = Path.home() / ".config" / "github-copilot" / "hosts.json"
        if copilot_hosts.exists():
            try:
                with open(copilot_hosts, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for host, creds in data.items():
                    tok = creds.get("oauth_token") or creds.get("token")
                    if tok:
                        return {
                            "token": tok,
                            "source": "copilot-hosts.json",
                            "enabled": True,
                        }
            except Exception:
                pass

        # 3. Fallback to 9Router & OmniRoute
        r9 = TokenFetcher.fetch_from_9router("copilot")
        if "copilot" in r9:
            return r9["copilot"]
        omni = TokenFetcher.fetch_from_omniroute("copilot")
        if "copilot" in omni:
            return omni["copilot"]

        return {}

    @staticmethod
    def fetch_claude_credentials() -> Dict[str, Any]:
        """
        Extract Anthropic / Claude Code CLI account state from ~/.claude.json.
        """
        claude_path = Path.home() / ".claude.json"
        if claude_path.exists():
            try:
                with open(claude_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                oa = data.get("oauthAccount", {})
                email = oa.get("emailAddress")
                # Check custom API keys if present
                custom_keys = data.get("customApiKeyResponses", {})
                api_key = data.get("primaryApiKey") or (list(custom_keys.values())[0] if custom_keys else None)
                if api_key:
                    return {
                        "api_key": api_key,
                        "email": email or "",
                        "source": "claude-cli",
                        "enabled": True,
                    }
            except Exception:
                pass

        # Fallback to 9Router or OmniRoute cline / claude
        r9 = TokenFetcher.fetch_from_9router("cline")
        if "cline" in r9:
            return r9["cline"]
        omni = TokenFetcher.fetch_from_omniroute("cline")
        if "cline" in omni:
            return omni["cline"]

        return {}

    @staticmethod
    def fetch_codex_credentials() -> Dict[str, Any]:
        """
        Extract Codex / OpenAI token from 9Router, OmniRoute or local config.
        """
        r9 = TokenFetcher.fetch_from_9router("codex")
        if "codex" in r9:
            return r9["codex"]
        omni = TokenFetcher.fetch_from_omniroute("codex")
        if "codex" in omni:
            return omni["codex"]
        return {}

    @staticmethod
    def fetch_for_provider(provider_id: str) -> Dict[str, Any]:
        """Unified resolver for any given provider."""
        p = provider_id.lower().replace("-", "_")
        if p == "cursor":
            creds = TokenFetcher.fetch_cursor_credentials()
            if not creds:
                r9 = TokenFetcher.fetch_from_9router("cursor")
                if "cursor" in r9:
                    return r9["cursor"]
            return creds
        elif p == "antigravity":
            creds = TokenFetcher.fetch_antigravity_credentials()
            if not creds:
                r9 = TokenFetcher.fetch_from_9router("antigravity")
                if "antigravity" in r9:
                    return r9["antigravity"]
            return creds
        elif p == "copilot":
            return TokenFetcher.fetch_copilot_credentials()
        elif p in ("claude", "cline"):
            creds = TokenFetcher.fetch_claude_credentials()
            if not creds:
                r9 = TokenFetcher.fetch_from_9router(p)
                if p in r9:
                    return r9[p]
                omni = TokenFetcher.fetch_from_omniroute(p)
                return omni.get(p, {})
            return creds
        elif p == "codex":
            return TokenFetcher.fetch_codex_credentials()
        elif p in ("command_code", "cmd", "command-code"):
            omni = TokenFetcher.fetch_from_omniroute("command_code")
            return omni.get("command_code", {})
        elif p in ("opencode_go", "opencode_zen"):
            r9 = TokenFetcher.fetch_from_9router()
            if "opencode_go" in r9 or "opencode_zen" in r9:
                return r9.get("opencode_go") or r9.get("opencode_zen") or {}
            omni = TokenFetcher.fetch_from_omniroute()
            return omni.get("opencode_go") or omni.get("opencode_zen") or {}
        else:
            # Check 9Router then OmniRoute
            r9 = TokenFetcher.fetch_from_9router(p)
            if p in r9:
                return r9[p]
            omni = TokenFetcher.fetch_from_omniroute(p)
            return omni.get(p, {})

    @staticmethod
    def fetch_all() -> Dict[str, Dict[str, Any]]:
        """Fetch credentials for all discoverable local providers."""
        found: Dict[str, Dict[str, Any]] = {}

        # 1. Probe 9Router store (~/.9router/db/data.sqlite)
        r9_all = TokenFetcher.fetch_from_9router()
        found.update(r9_all)

        # 2. Probe OmniRoute store (~/.omniroute/storage.sqlite)
        omni_all = TokenFetcher.fetch_from_omniroute()
        for k, v in omni_all.items():
            if k not in found:
                found[k] = v

        # 3. Probe native IDEs and CLIs (prefer freshest native session if present)
        cursor_creds = TokenFetcher.fetch_cursor_credentials()
        if cursor_creds:
            found["cursor"] = cursor_creds

        ag_creds = TokenFetcher.fetch_antigravity_credentials()
        if ag_creds:
            found["antigravity"] = ag_creds

        copilot_creds = TokenFetcher.fetch_copilot_credentials()
        if copilot_creds:
            found["copilot"] = copilot_creds

        claude_creds = TokenFetcher.fetch_claude_credentials()
        if claude_creds:
            found["claude"] = claude_creds
            found["cline"] = claude_creds

        return found
