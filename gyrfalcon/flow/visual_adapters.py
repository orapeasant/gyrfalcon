"""AI agent, A2A, and Notification adapters for visual flow Activities."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from gyrfalcon.flow.graph_context import ActivityContext, ActivityResult, WaitResult


def activity_dispatcher(store, node: dict, context: ActivityContext, visit_id: str):
    implementation = node.get("implementation") or node.get("type")
    config = node.get("agent") or {}
    if implementation == "agent":
        return _gyrfalcon_agent(store, node, config, context, visit_id)
    if implementation == "a2a":
        return _a2a_agent(store, node, config, context, visit_id)
    raise ValueError(f"Unsupported visual Activity implementation {implementation!r}")


def _selected_context(node: dict, context: ActivityContext) -> dict[str, Any]:
    config = node.get("agent") or {}
    mode = config.get("contextMode", config.get("context_mode", "full"))
    if mode == "prior":
        prior = config.get("priorNodeId") or config.get("prior_node_id")
        if not prior or prior not in context.outputs:
            raise ValueError("Agent Activity references an unavailable prior node output")
        selected = {"prior_node_id": prior, "output": context.outputs[prior]}
    elif mode == "full":
        selected = context.context
    else:
        raise ValueError("Agent Activity contextMode must be 'full' or 'prior'")
    return {"flow": selected, "attributes": {**context.flow_attributes, **context.attributes},
            "incoming": context.incoming_value, "human_response": context.human_response}


def _result_from_text(text: str, node: dict) -> ActivityResult | WaitResult:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Agent Activity must return a JSON object with transient and output") from exc
    if not isinstance(value, dict):
        raise ValueError("Agent Activity response must be a JSON object")
    if value.get("wait") is True:
        return WaitResult()
    transient = value.get("transient")
    if not isinstance(transient, str) or transient not in node.get("transients", []):
        raise ValueError("Agent Activity returned an undeclared transient")
    return ActivityResult(transient=transient, output=value.get("output", value.get("value")),
                          attributes=value.get("attributes") or {})


def _gyrfalcon_agent(store, node: dict, config: dict,
                     context: ActivityContext, visit_id: str):
    from gyrfalcon.agents import agent_config_version, build_agent_kwargs, get_agent
    from gyrfalcon.gyrfalcon_state import SessionDB
    from gyrfalcon.run_agent import AIAgent

    agent_id = config.get("id")
    agent = get_agent(agent_id) if agent_id else None
    if agent is None or not agent.get("enabled", True):
        raise ValueError(f"Pinned Gyrfalcon agent {agent_id!r} is unavailable")
    actual_version = agent_config_version(agent)
    if config.get("version") != actual_version:
        raise ValueError(f"Pinned agent version mismatch for {agent_id!r}")
    kwargs = build_agent_kwargs(agent)
    session_id = store.create_visit_session(
        context.run_id, visit_id, "flow_agent", agent_id=agent_id, model=kwargs.get("model"))
    session_db = SessionDB()
    try:
        store.update_visit_session(context.run_id, visit_id, "running")
        prompt_data = _selected_context(node, context)
        transients = node.get("transients", [])
        prompt = (
            "You are executing one Activity inside a durable visual flow.\n"
            "Return only a JSON object with keys 'transient' and 'output'. "
            "The transient must be exactly one of the declared values. If you need a human reply, "
            "return {\"wait\":true} and ask your question in the assistant response. "
            "After the human reply, continue this same session and return the final object.\n"
            f"Declared transients: {json.dumps(transients)}\n"
            f"Activity instructions: {node.get('instructions') or node.get('label') or node['id']}\n"
            f"Flow data: {json.dumps(prompt_data, ensure_ascii=False, default=str)}"
        )
        if context.human_response is not None:
            prompt = "Human response for the pending flow Activity:\n" + json.dumps(
                context.human_response, ensure_ascii=False, default=str)
        result = AIAgent(**kwargs, quiet_mode=True, skip_memory=True, platform="flow",
                         session_id=session_id, session_db=session_db).run_conversation(
                             user_message=prompt)
        response = result.get("final_response", "")
        activity_result = _result_from_text(response, node)
        store.update_visit_session(context.run_id, visit_id,
                                   "waiting" if isinstance(activity_result, WaitResult) else "completed")
        return activity_result
    except Exception as exc:
        store.update_visit_session(context.run_id, visit_id, "failed",
                                   {"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        session_db.close()


def _a2a_agent(store, node: dict, config: dict,
               context: ActivityContext, visit_id: str):
    import httpx
    from gyrfalcon.sessions.store import SessionStore

    url = config.get("url") or config.get("endpoint")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValueError("A2A Activity needs an HTTP(S) endpoint")
    headers = {"Content-Type": "application/json", "A2A-Version": "1.0"}
    secret_name = config.get("bearerSecret") or config.get("bearer_secret")
    if secret_name:
        from gyrfalcon.security import get_stored_secret
        token = get_stored_secret(secret_name)
        if not token:
            raise ValueError(f"A2A bearer secret {secret_name!r} is unavailable")
        headers["Authorization"] = f"Bearer {token}"
    card_url = config.get("cardUrl") or config.get("card_url")
    if not card_url:
        raise ValueError("A2A Activity needs cardUrl to verify its published Agent Card version")
    card_response = httpx.get(card_url, headers=headers, timeout=15.0)
    card_response.raise_for_status()
    card = card_response.json()
    if card.get("version") != config.get("version"):
        raise ValueError("Pinned A2A Agent Card version is unavailable")
    endpoint = next((item.get("url") for item in card.get("supportedInterfaces", [])
                     if item.get("protocolBinding") == "JSONRPC"), None)
    if endpoint:
        url = endpoint
    message = json.dumps({"flow": _selected_context(node, context),
                          "transients": node.get("transients", [])},
                         ensure_ascii=False, default=str)
    if context.human_response is not None:
        message = json.dumps({"human_response": context.human_response},
                             ensure_ascii=False, default=str)
    session_id = store.create_visit_session(context.run_id, visit_id,
                                             "flow_agent", agent_id=config.get("id"))
    sessions = SessionStore()
    try:
        store.update_visit_session(context.run_id, visit_id, "running")
        sessions.append_message(session_id, "user", message)
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "SendMessage",
                   "params": {"message": {"messageId": str(uuid.uuid4()),
                                            "contextId": session_id, "role": "user",
                                            "parts": [{"text": message}]},
                              "configuration": {"acceptedOutputModes": ["text"],
                                                "blocking": True}}}
        response = httpx.post(url, json=payload, headers=headers, timeout=120.0)
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise ValueError(f"A2A agent returned error: {body['error']}")
        result = body.get("result")
        if not isinstance(result, dict):
            raise ValueError("A2A response did not include a result")
        remote_task = result.get("task") if isinstance(result.get("task"), dict) else result
        task_status = remote_task.get("status", {})
        if isinstance(task_status, dict) and task_status.get("state") == "input-required":
            text = _a2a_text(remote_task)
            sessions.append_message(session_id, "assistant", text)
            store.update_visit_session(context.run_id, visit_id, "waiting")
            return WaitResult()
        output_text = _a2a_text(result)
        sessions.append_message(session_id, "assistant", output_text)
        activity_result = _result_from_text(output_text, node)
        store.update_visit_session(context.run_id, visit_id,
                                   "waiting" if isinstance(activity_result, WaitResult) else "completed")
        return activity_result
    except Exception as exc:
        store.update_visit_session(context.run_id, visit_id, "failed",
                                   {"type": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        sessions.close()


def _a2a_text(value: dict) -> str:
    pieces = []
    for field in ("artifacts", "parts"):
        entries = value.get(field)
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    if "text" in entry:
                        pieces.append(str(entry["text"]))
                    for part in entry.get("parts", []) if isinstance(entry.get("parts"), list) else []:
                        if isinstance(part, dict) and "text" in part:
                            pieces.append(str(part["text"]))
    if isinstance(value.get("message"), dict):
        pieces.append(_a2a_text(value["message"]))
    return "\n".join(pieces)


def notification_dispatcher(store, node: dict, context: ActivityContext,
                            visit_id: str, notification_snapshot: dict):
    config = node.get("notification") or {}
    template_id = config.get("templateId") or config.get("template_id")
    template = notification_snapshot.get(template_id)
    if template is None:
        raise ValueError(f"Pinned Notification template {template_id!r} is unavailable")
    session_id = store.create_visit_session(context.run_id, visit_id,
                                             "flow_notification",
                                             agent_id=template.get("agent_id"))
    from gyrfalcon.sessions.store import SessionStore
    sessions = SessionStore()
    try:
        store.update_visit_session(context.run_id, visit_id, "running")
        subject = _render(template.get("subject_template") or "", context)
        body = _render(template.get("body_template") or "", context)
        channel = template.get("channel") or "dashboard"
        notif_config = template.get("config") or {}
        html_template = notif_config.get("body_html_template") or ""
        body_html = _render(html_template, context) if html_template else None
        external_message_id = None
        if channel == "email":
            from email.utils import make_msgid
            external_message_id = make_msgid()
        recipients = {
            "kind": template["recipient_kind"],
            "ref": template["recipient_ref"],
            "to": notif_config.get("to") or ([template["recipient_ref"]] if channel == "email" else []),
            "cc": notif_config.get("cc", []),
            "bcc": notif_config.get("bcc", []),
        }
        message_id = sessions.append_flow_message(
            session_id, visit_id, content=body, message_kind="notification",
            channel=channel, direction="outbound", subject=subject or None,
            sender=notif_config.get("from"), recipients=recipients,
            body_html=body_html, external_message_id=external_message_id,
        )
        delivered = False
        delivery_error = None
        provider_id = None
        try:
            delivery_template = {**template, "config": {**notif_config,
                "body_html_template": body_html,
                "message_id": external_message_id}}
            provider_id = _deliver(channel, delivery_template, subject, body)
            delivered = True
        except Exception as exc:
            delivery_error = {"type": type(exc).__name__, "message": str(exc)}
        store.record_delivery(
            run_id=context.run_id, visit_id=visit_id, message_id=message_id,
            recipient_kind=template["recipient_kind"], recipient_ref=template["recipient_ref"],
            channel=channel, state="delivered" if delivered else "failed",
            provider_id=provider_id, error=delivery_error,
        )
        if not delivered:
            store.update_visit_session(context.run_id, visit_id, "failed", delivery_error)
            raise RuntimeError(f"Notification delivery failed: {delivery_error['message']}")
        if config.get("mode") == "wait":
            timeout = config.get("timeoutSeconds", config.get("timeout_seconds"))
            deadline = time.time() + float(timeout)
            store.update_node_visit(visit_id,
                                    timeout_transient=config.get("timeoutTransient", config.get("timeout_transient")))
            store.update_visit_session(context.run_id, visit_id, "waiting")
            return WaitResult(deadline=deadline)
        if len(node.get("transients", [])) != 1:
            raise ValueError("Send-and-continue Notification must have one transient")
        store.update_visit_session(context.run_id, visit_id, "completed")
        return ActivityResult(node["transients"][0], {"message_id": message_id})
    finally:
        sessions.close()


def _render(template: str, context: ActivityContext) -> str:
    values = {"incoming": context.incoming_value, "inputs": context.inputs,
              "outputs": context.outputs, "attributes": context.attributes,
              "flow_attributes": context.flow_attributes}
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{{ " + key + " }}", json.dumps(value, default=str))
    return rendered


def _deliver(channel: str, template: dict, subject: str, body: str) -> str | None:
    if channel in {"dashboard", "internal", "chat"}:
        from gyrfalcon.gateway.delivery import deliver_from_anywhere, parse_target

        target = template.get("recipient_ref", "")
        if channel == "chat" and parse_target(target) is not None:
            result = deliver_from_anywhere(target, body)
            if not result.ok:
                raise RuntimeError(result.detail)
            return result.detail
        # Dashboard/internal delivery is durable in ai_session_messages and
        # addressed to the configured principal/group/role.
        return None
    if channel == "email":
        config = template.get("config") or {}
        smtp_host = config.get("smtp_host")
        if not smtp_host:
            raise RuntimeError("Email Notification has no SMTP transport configuration")
        import smtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["Subject"] = subject
        if config.get("message_id"):
            message["Message-ID"] = config["message_id"]
        message["From"] = config.get("from") or "gyrfalcon@localhost"
        recipients = config.get("to")
        if not recipients and template.get("recipient_kind") == "user":
            from gyrfalcon.auth.store import get_auth_store
            user = get_auth_store().get_user(template["recipient_ref"])
            recipients = [user["email"]] if user and user.get("email") else []
        if not recipients:
            raise RuntimeError("Email recipient has no resolved address; configure recipient to/cc/bcc")
        if isinstance(recipients, str):
            recipients = [recipients]
        message["To"] = ", ".join(recipients)
        if config.get("cc"):
            message["Cc"] = ", ".join(config["cc"] if isinstance(config["cc"], list) else [config["cc"]])
        if config.get("bcc"):
            message["Bcc"] = ", ".join(config["bcc"] if isinstance(config["bcc"], list) else [config["bcc"]])
        message.set_content(body)
        if config.get("body_html_template"):
            message.add_alternative(config.get("body_html_template"), subtype="html")
        with smtplib.SMTP(smtp_host, int(config.get("smtp_port", 25)),
                          timeout=float(config.get("timeout", 30))) as smtp:
            if config.get("starttls"):
                smtp.starttls()
            if config.get("username"):
                password = config.get("password")
                if not password and config.get("smtp_secret"):
                    from gyrfalcon.security import get_stored_secret
                    password = get_stored_secret(config["smtp_secret"])
                if not password:
                    raise RuntimeError("SMTP username is set but its password secret is unavailable")
                smtp.login(config["username"], password)
            refused = smtp.send_message(message)
        if refused:
            raise RuntimeError(f"SMTP rejected recipients: {refused}")
        return message.get("Message-ID")
    raise RuntimeError(f"No Notification transport configured for channel {channel!r}")
