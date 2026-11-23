"""Negotiate a text-generation interface without guessing capabilities from model names."""

import json
import os

from openai import APIStatusError, OpenAI
from pydantic import Field

from .models import Model


class Action(Model):
    name: str
    arguments: dict


class Actions(Model):
    actions: list[Action] = Field(min_length=1, max_length=8)


def client():
    if os.getenv("TRAVEL_LLM_PROVIDER", "openai") == "ollama":
        return OpenAI(
            api_key="ollama",
            base_url="http://127.0.0.1:11434/v1",
            timeout=65,
            max_retries=1,
        )
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("Configure OPENAI_API_KEY on the backend.")
    return OpenAI(timeout=65, max_retries=1)


class Dialogue:
    def __init__(self, ai, model, system, context, schemas):
        self.ai, self.model, self.system, self.schemas = ai, model, system, schemas
        self.modes = ["responses", "chat", "responses-json", "chat-json"]
        if os.getenv("TRAVEL_LLM_PROVIDER") == "ollama":
            self.modes = ["chat", "chat-json"]
        requested = os.getenv("OPENAI_API_MODE", "auto")
        if requested != "auto":
            if requested not in self.modes:
                raise ValueError("Unknown OPENAI_API_MODE for this provider.")
            self.modes = [requested]
        self.context = [
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}
        ]
        self.previous = None
        self.outputs = []
        self.messages = []
        self.mode = None

    def step(self):
        while self.modes:
            mode = self.modes[0]
            if self.mode != mode:
                self.previous = None
                self.messages = [
                    {"role": "system", "content": self.system},
                    *self.context,
                ]
                self.mode = mode
            try:
                if mode == "responses":
                    payload = (
                        {"previous_response_id": self.previous, "input": self.outputs}
                        if self.previous
                        else {"input": self.context}
                    )
                    reply = self.ai.responses.create(
                        model=self.model,
                        instructions=self.system,
                        tools=[
                            {"type": "function", **s, "strict": False}
                            for s in self.schemas
                        ],
                        tool_choice="required",
                        **payload,
                    )
                    self.previous = reply.id
                    self.outputs = []
                    return [
                        (c.call_id, c.name, c.arguments)
                        for c in reply.output
                        if c.type == "function_call"
                    ]
                if mode == "chat":
                    r = self.ai.chat.completions.create(
                        model=self.model,
                        messages=self.messages,
                        tools=[
                            {"type": "function", "function": s} for s in self.schemas
                        ],
                    )
                    msg = r.choices[0].message
                    self.messages.append(
                        msg.model_dump(
                            include={"role", "content", "tool_calls"}, exclude_none=True
                        )
                    )
                    return [
                        (c.id, c.function.name, c.function.arguments)
                        for c in msg.tool_calls or []
                    ]
                instruction = (
                    self.system
                    + ' Return JSON only: {"actions":[{"name":"tool","arguments":{}}]}. Tools: '
                    + json.dumps(self.schemas)
                )
                # A user-only transcript also works for older models without a system role.
                prompt = (
                    instruction
                    + "\nConversation and executed tool results:\n"
                    + json.dumps(self.context)
                )
                if mode == "responses-json":
                    content = self.ai.responses.create(
                        model=self.model, input=prompt
                    ).output_text
                else:
                    content = (
                        self.ai.chat.completions.create(
                            model=self.model,
                            messages=[{"role": "user", "content": prompt}],
                        )
                        .choices[0]
                        .message.content
                    )
                try:
                    raw = (content or "").strip()
                    if raw.startswith("```") and raw.endswith("```"):
                        raw = raw.partition("\n")[2][:-3].strip()
                    actions = Actions.model_validate_json(raw).actions
                except ValueError:
                    self.feedback("Invalid JSON. Return the required actions object.")
                    return []
                return [
                    (str(i), a.name, json.dumps(a.arguments))
                    for i, a in enumerate(actions)
                ]
            except APIStatusError as exc:
                if exc.status_code not in (400, 404, 405, 422):
                    raise
                self.modes.pop(0)
        raise ValueError(
            "Selected model rejected all supported text and function-calling interfaces."
        )

    def feedback(self, message):
        entry = {"role": "user", "content": message}
        self.context.append(entry)
        self.messages.append(entry)
        self.outputs.append(entry)

    def accept(self, call, result):
        cid, name, args = call
        content = json.dumps(result, ensure_ascii=False)
        self.context.append(
            {
                "role": "user",
                "content": json.dumps(
                    {"executed": name, "arguments": args, "result": result}
                ),
            }
        )
        if self.mode == "responses":
            self.outputs.append(
                {"type": "function_call_output", "call_id": cid, "output": content}
            )
        elif self.mode == "chat":
            self.messages.append(
                {"role": "tool", "tool_call_id": cid, "content": content}
            )
