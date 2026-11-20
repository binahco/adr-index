from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml
from cache_ratelimit import CachedProvider, RateLimiter, RealClock, ThrottledProvider
from llm_client import CompletionRequest, CompletionResult, LlmClient, ReplayProvider, Span
from llm_client.providers.opencode_cli import OpenCodeCLI
from parser_io import parse
from schema_validate import SchemaRegistry
from secure_base import sanitize_for_prompt
from test_kit import EvalCase, EvalDataset, run

from .models import AnswerCard

DEFAULT_MODEL = "opencode/big-pickle"
SCHEMA_ID = "answer-card-v1"
PROMPT_ID = "adr-answerer"
ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT.parent / "llm-dev-core"
DEFAULT_PROMPT = ROOT / "prompts" / "adr-answerer.md"
DEFAULT_DATASET = ROOT / "evals" / "adr-answerer.jsonl"
BODY_CHARS = 400
TOP_SECTIONS = 6
STOPWORDS = {"que", "los", "las", "del", "una", "como", "cómo", "para", "con", "por", "donde", "cuál", "cuando", "cuándo"}


@dataclass(frozen=True)
class IndexedSection:
    file: str
    section: str
    line_start: int
    line_end: int
    body: str


# --------------------------------------------------------------------------
# índice léxico determinista (lo semántico es vector-core, C11/issue #2)
# --------------------------------------------------------------------------
def default_docs() -> list[Path]:
    docs = sorted((CORE / "docs" / "decisions").glob("*.md"))
    docs += sorted((CORE / "docs" / "runbooks").glob("*.md"))
    docs.append(CORE / "ARCHITECTURE.md")
    return [d for d in docs if d.is_file()]


def build_index(paths: list[Path]) -> list[IndexedSection]:
    out: list[IndexedSection] = []
    for path in paths:
        doc = parse(path)
        for s in doc.sections:
            out.append(
                IndexedSection(
                    file=path.name,
                    section=s.title,
                    line_start=s.line_start,
                    line_end=s.line_end,
                    body=s.body,
                )
            )
    return out


def _terms(query: str) -> list[str]:
    raw = re.split(r"[^a-záéíóúñ0-9]+", query.lower())
    return [t for t in raw if len(t) >= 3 and t not in STOPWORDS]


def search(index: list[IndexedSection], query: str, *, top: int = TOP_SECTIONS) -> list[IndexedSection]:
    """Ranking léxico determinista: título vale 3, cuerpo cuenta ocurrencias."""
    terms = _terms(query)
    scored: list[tuple[int, IndexedSection]] = []
    for sec in index:
        title, body = sec.section.lower(), sec.body.lower()
        score = 0
        for term in terms:
            score += 3 if term in title else 0
            score += body.count(term)
        if score > 0:
            scored.append((score, sec))
    scored.sort(key=lambda p: (-p[0], p[1].file, p[1].section))
    return [sec for _, sec in scored[:top]]


def format_context(sections: list[IndexedSection]) -> str:
    bloques = [
        f"[{s.file} · «{s.section}» · líneas {s.line_start}-{s.line_end}]\n{s.body[:BODY_CHARS]}"
        for s in sections
    ]
    return "\n\n".join(bloques) if bloques else "(el índice no encontró secciones relevantes)"


def _norm(text: str) -> str:
    return text.lower().replace("«", "").replace("»", "").replace("§", "").strip()


def citations_valid(card: AnswerCard, index: list[IndexedSection]) -> list[str]:
    """Cada cita debe existir en el índice: (archivo, sección) reales, nada inventado."""
    conocidas = {(s.file, _norm(s.section)) for s in index}
    problemas = []
    for cita in card.citations:
        if (cita.file, _norm(cita.section)) not in conocidas:
            problemas.append(f"{cita.file} · «{cita.section}» no está en el índice")
    return problemas


# --------------------------------------------------------------------------
# LLM
# --------------------------------------------------------------------------
def load_prompt(path: Path) -> tuple[str, str, str, Path]:
    text = path.read_text()
    if not text.startswith("---"):
        raise SystemExit(f"{path}: falta frontmatter")
    _, frontmatter, body = text.split("---", 2)
    data = yaml.safe_load(frontmatter)
    eval_path = ROOT / data["eval"] if not Path(data["eval"]).is_absolute() else Path(data["eval"])
    return data["id"], data["version"], body.strip(), eval_path


def render_answerer(prompt_id: str, prompt_version: str, variables: dict) -> list[dict]:
    if prompt_id == PROMPT_ID:
        _, _, body, _ = load_prompt(DEFAULT_PROMPT)
        system_part = body.split("## Sistema\n", 1)[1].split("## Usuario\n", 1)[0].strip()
        user_part = body.split("## Usuario\n", 1)[1].strip().format(**variables)
        return [
            {"role": "system", "content": system_part},
            {"role": "user", "content": user_part},
        ]
    payload = {"prompt_id": prompt_id, "prompt_version": prompt_version, "variables": variables}
    return [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def build_emitter(span_file: Path | None):
    def emit(span: Span, _result) -> None:
        if span_file is not None:
            with span_file.open("a") as handle:
                handle.write(span.as_jsonl() + "\n")
        else:
            sys.stderr.write(span.as_jsonl() + "\n")

    return emit


def build_client(inner_provider, *, clock=None, span_file: Path | None = None) -> dict:
    clock = RealClock() if clock is None else clock
    cached = CachedProvider(inner_provider, ttl_seconds=3600.0, clock=clock)
    limiter = RateLimiter(rpm=120, burst=20, clock=clock)
    provider = ThrottledProvider(cached, limiter)
    registry = SchemaRegistry()
    registry.register(SCHEMA_ID, AnswerCard)
    client = LlmClient(
        provider,
        consumer_repo="adr-index",
        model_aliases={"fast": DEFAULT_MODEL},
        renderer=render_answerer,
        validator=registry.make_validator(SCHEMA_ID),
        emitter=build_emitter(span_file),
    )
    return {"client": client, "cached": cached, "limiter": limiter}


def ask(client: LlmClient, index: list[IndexedSection], question: str) -> AnswerCard:
    """Pregunta → ranking léxico → respuesta LLM con citas verificadas contra el índice."""
    safe_q = sanitize_for_prompt(question).text.replace("{", "(").replace("}", ")")
    contexto = format_context(search(index, safe_q))
    result = client.complete(
        CompletionRequest(
            prompt_id=PROMPT_ID,
            prompt_version="0.1.0",
            variables={"question": safe_q, "context": contexto.replace("{", "(").replace("}", ")")},
            model_alias="fast",
            response_schema=SCHEMA_ID,
            tags=["adr-index", "week-20"],
        )
    )
    if not result.validation.ok:
        raise RuntimeError("; ".join(result.validation.errors))
    card = result.parsed
    problemas = citations_valid(card, index)
    if problemas:
        raise RuntimeError("citas inventadas (rechazado): " + "; ".join(problemas))
    return card


def judge_for(client: LlmClient) -> Callable[[EvalCase], CompletionResult]:
    """El dataset congela pregunta+contexto: el replay no caduca cuando los docs cambian."""

    def judge(case: EvalCase) -> CompletionResult:
        return client.complete(
            CompletionRequest(
                prompt_id=case.prompt_id,
                prompt_version=case.prompt_version,
                variables=case.input,
                model_alias="fast",
                response_schema=SCHEMA_ID,
                tags=["adr-index", "week-20"],
            )
        )

    return judge


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="adr-index", description="Búsqueda en los ADRs del core con citas verificadas (semana 20).")
    parser.add_argument("command", nargs="?", default="ask", choices=["ask", "search", "record", "replay-check"])
    parser.add_argument("question", nargs="*", help="ask/search: la pregunta o términos")
    args = parser.parse_args(argv)
    question = " ".join(args.question).strip()

    if args.command == "record":
        dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
        provider = ReplayProvider(ROOT / "cassettes", record=True, inner=OpenCodeCLI(DEFAULT_MODEL))
        report = run(dataset, judge_for(build_client(provider)["client"]), mode="full", threshold=1.0)
        print(f"grabadas {len(dataset.cases)} respuestas; pass={report.passed}/{report.total}")
        return 0 if report.threshold_ok else 1

    if args.command == "replay-check":
        dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
        provider = ReplayProvider(ROOT / "cassettes", record=False)
        report = run(dataset, judge_for(build_client(provider)["client"]), mode="full", threshold=1.0)
        print(f"replay: pass={report.passed}/{report.total} threshold_ok={report.threshold_ok}")
        return 0 if report.threshold_ok else 1

    index = build_index(default_docs())
    if args.command == "search":
        for sec in search(index, question):
            print(f"{sec.file} · «{sec.section}» · líneas {sec.line_start}-{sec.line_end}")
        return 0

    if not question:
        parser.error("ask exige una pregunta")
    card = ask(build_client(OpenCodeCLI(DEFAULT_MODEL))["client"], index, question)
    print(json.dumps(card.model_dump(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
