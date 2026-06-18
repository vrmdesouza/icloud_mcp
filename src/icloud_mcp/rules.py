"""Local rules engine for automatic email filtering via JSON-stored rules."""

from __future__ import annotations

import fcntl
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from icloud_mcp.models import Email, Rule, RuleAction, RuleCondition

if TYPE_CHECKING:
    from icloud_mcp.imap_client import IMAPClient

log = logging.getLogger(__name__)

VALID_FIELDS = {"sender", "subject", "body"}
VALID_OPERATORS = {"equals", "contains", "starts_with", "ends_with"}
VALID_ACTIONS = {"move", "flag", "mark_as_read", "delete"}


class RulesEngine:
    """Manages email filtering rules stored in a local JSON file.

    Rules are persisted at ``<rules_dir>/rules.json``.  The default directory
    is ``~/.icloud_mcp``, but a custom path can be injected for testing.

    Args:
        rules_dir: Directory where ``rules.json`` is stored.  Defaults to
            ``~/.icloud_mcp``.
    """

    def __init__(self, rules_dir: Path | None = None) -> None:
        self._rules_file = (rules_dir or Path.home() / ".icloud_mcp") / "rules.json"
        self._rules: list[Rule] = []
        self._load_rules()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_rules(self) -> None:
        """Load rules from disk, creating the directory if needed.

        Uses a shared file lock (``LOCK_SH``) to prevent reading while
        another process is writing.
        """
        self._rules_file.parent.mkdir(parents=True, exist_ok=True)
        if self._rules_file.exists():
            try:
                with open(self._rules_file, encoding="utf-8") as f:
                    fcntl.flock(f, fcntl.LOCK_SH)
                    try:
                        raw = json.load(f)
                    finally:
                        fcntl.flock(f, fcntl.LOCK_UN)
                self._rules = [Rule.model_validate(r) for r in raw]
                log.info("Regras carregadas: %d regra(s) encontrada(s).", len(self._rules))
            except (json.JSONDecodeError, Exception):
                log.warning("Arquivo de regras corrompido. Iniciando com lista vazia.")
                self._rules = []
        else:
            self._rules = []
            log.info("Nenhum arquivo de regras encontrado. Iniciando com lista vazia.")

    def _save_rules(self) -> None:
        """Persist all rules to disk as JSON.

        Uses an exclusive file lock (``LOCK_EX``) to prevent concurrent writes.
        """
        data = [r.model_dump() for r in self._rules]
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        with open(self._rules_file, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                f.write(payload)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def list_rules(self) -> list[Rule]:
        """Return a copy of all rules, ordered by creation date.

        Returns:
            Sorted list of ``Rule`` instances.
        """
        return sorted(self._rules, key=lambda r: r.created_at or "")

    def create_rule(
        self,
        name: str,
        conditions: list[dict[str, str]],
        actions: list[dict[str, str | None]],
    ) -> Rule:
        """Create and persist a new filtering rule.

        Args:
            name: Unique rule name.
            conditions: Each dict must have ``field``, ``operator``, ``value``.
            actions: Each dict must have ``action_type`` and optionally ``destination``.

        Returns:
            The newly created ``Rule``.

        Raises:
            ValueError: On duplicate name or invalid field/operator/action.
        """
        # Duplicate name check
        if any(r.name == name for r in self._rules):
            msg = f"Regra com nome '{name}' já existe."
            raise ValueError(msg)

        # Validate & build conditions
        parsed_conditions: list[RuleCondition] = []
        for c in conditions:
            field = c.get("field", "")
            if field not in VALID_FIELDS:
                msg = f"Campo inválido: '{field}'. Válidos: sender, subject, body"
                raise ValueError(msg)
            operator = c.get("operator", "")
            if operator not in VALID_OPERATORS:
                msg = (
                    f"Operador inválido: '{operator}'. "
                    "Válidos: equals, contains, starts_with, ends_with"
                )
                raise ValueError(msg)
            parsed_conditions.append(
                RuleCondition(
                    field=field,  # type: ignore[arg-type]
                    operator=operator,  # type: ignore[arg-type]
                    value=c.get("value", ""),
                )
            )

        # Validate & build actions
        parsed_actions: list[RuleAction] = []
        for a in actions:
            action_type = a.get("action_type", "")
            if action_type not in VALID_ACTIONS:
                msg = (
                    f"Tipo de ação inválido: '{action_type}'. "
                    "Válidos: move, flag, mark_as_read, delete"
                )
                raise ValueError(msg)
            destination = a.get("destination")
            if action_type == "move" and not destination:
                msg = "Parâmetro 'destination' obrigatório para ação 'move'."
                raise ValueError(msg)
            parsed_actions.append(
                RuleAction(
                    action_type=action_type,  # type: ignore[arg-type]
                    destination=destination,
                )
            )

        rule = Rule(
            name=name,
            conditions=parsed_conditions,
            actions=parsed_actions,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._rules.append(rule)
        self._save_rules()
        log.info("Regra '%s' criada com sucesso.", name)
        return rule

    def delete_rule(self, name: str) -> dict[str, str]:
        """Delete a rule by name.

        Args:
            name: Name of the rule to delete.

        Returns:
            Confirmation dict with ``status`` and ``name``.

        Raises:
            ValueError: If the rule is not found.
        """
        for i, r in enumerate(self._rules):
            if r.name == name:
                self._rules.pop(i)
                self._save_rules()
                log.info("Regra '%s' removida.", name)
                return {"status": "deleted", "name": name}
        msg = f"Regra '{name}' não encontrada."
        raise ValueError(msg)

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    async def apply_rules(self, folder: str, imap_client: IMAPClient) -> dict[str, Any]:
        """Apply all enabled rules to emails in a folder.

        Iterates in batches of 100 emails.  When a ``move`` or ``delete``
        action is triggered for an email, subsequent actions and rules are
        skipped for that email (the UID is no longer in the folder).

        Args:
            folder: IMAP folder to scan.
            imap_client: Live ``IMAPClient`` instance.

        Returns:
            Stats dict with ``processed``, ``matched``, ``actions_applied``.
        """
        enabled_rules = [r for r in self._rules if r.enabled]
        if not enabled_rules:
            log.info("Nenhuma regra ativa para aplicar.")
            return {"processed": 0, "matched": 0, "actions_applied": 0}

        processed = 0
        matched = 0
        actions_applied = 0
        batch_size = 100
        seen_uids: set[str] = set()

        while True:
            result = await imap_client.list_emails(folder=folder, limit=batch_size, offset=0)
            if not result.emails:
                break

            new_emails = [e for e in result.emails if e.uid not in seen_uids]
            if not new_emails:
                break

            for email_obj in new_emails:
                seen_uids.add(email_obj.uid)
                processed += 1
                email_matched = False
                skip_email = False

                for rule in enabled_rules:
                    if skip_email:
                        break
                    if self._email_matches_rule(email_obj, rule):
                        if not email_matched:
                            matched += 1
                            email_matched = True

                        for action in rule.actions:
                            if skip_email:
                                break
                            try:
                                await self._execute_action(action, email_obj, folder, imap_client)
                                actions_applied += 1
                            except Exception:
                                log.exception(
                                    "Erro ao executar ação '%s' no email UID %s.",
                                    action.action_type,
                                    email_obj.uid,
                                )
                            if action.action_type in ("move", "delete"):
                                skip_email = True

            if len(result.emails) < batch_size:
                break

        log.info(
            "Regras aplicadas: %d email(s) processado(s), %d casamento(s), %d ação(ões).",
            processed,
            matched,
            actions_applied,
        )
        return {"processed": processed, "matched": matched, "actions_applied": actions_applied}

    # ------------------------------------------------------------------
    # Matching helpers
    # ------------------------------------------------------------------

    def _email_matches_rule(self, email_obj: Email, rule: Rule) -> bool:
        """Check if an email matches all conditions of a rule (AND logic)."""
        if not rule.conditions:
            return False
        return all(self._matches_condition(email_obj, c) for c in rule.conditions)

    def _matches_condition(self, email_obj: Email, condition: RuleCondition) -> bool:
        """Evaluate a single condition against an email (case-insensitive)."""
        field_map: dict[str, str] = {
            "sender": email_obj.sender,
            "subject": email_obj.subject,
            "body": email_obj.body_text,
        }
        field_value = field_map.get(condition.field, "").lower()
        cond_value = condition.value.lower()

        if condition.operator == "equals":
            return field_value == cond_value
        if condition.operator == "contains":
            return cond_value in field_value
        if condition.operator == "starts_with":
            return field_value.startswith(cond_value)
        if condition.operator == "ends_with":
            return field_value.endswith(cond_value)
        return False

    async def _execute_action(
        self,
        action: RuleAction,
        email_obj: Email,
        folder: str,
        imap_client: IMAPClient,
    ) -> None:
        """Execute a single rule action on an email."""
        if action.action_type == "mark_as_read":
            await imap_client.mark_as_read(folder=folder, uid=email_obj.uid)
        elif action.action_type == "flag":
            await imap_client.flag_email(folder=folder, uid=email_obj.uid)
        elif action.action_type == "move" and action.destination:
            await imap_client.move_email(
                folder=folder, uid=email_obj.uid, destination=action.destination
            )
        elif action.action_type == "delete":
            await imap_client.delete_email(folder=folder, uid=email_obj.uid)
        else:
            log.warning("Tipo de ação desconhecido: '%s'. Ignorando.", action.action_type)
