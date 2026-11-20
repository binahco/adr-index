from __future__ import annotations

import json
from pathlib import Path

from llm_client.provider import ProviderRequest, ProviderResponse
from test_kit import EvalDataset, run as run_evalset

from adr_index.main import (
    DEFAULT_DATASET,
    IndexedSection,
    build_client,
    build_index,
    citations_valid,
    format_context,
    judge_for,
    search,
)
from adr_index.models import AnswerCard, Citation

STUB_TEXT = json.dumps(
    {
        "question": "¿q?",
        "answer": "a",
        "citations": [{"file": "0008-bot-base-contract.md", "section": "2. Reglas", "line_start": 32}],
        "confidence": "alta",
    },
    ensure_ascii=False,
)


class StubProvider:
    name = "stub"

    def __init__(self, text: str | None = None) -> None:
        self.text = text or STUB_TEXT
        self.calls = 0
        self.last_request: ProviderRequest | None = None

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        self.calls += 1
        self.last_request = request
        return ProviderResponse(text=self.text, model=request.model)

    def stream(self, request: ProviderRequest) -> list[str]:
        self.calls += 1
        return [self.text]


FIXTURES = Path(__file__).resolve().parents[1] / "tests"


def _mini_docs(tmp_path: Path) -> list[Path]:
    a = tmp_path / "0001-ejemplo.md"
    a.write_text("# Título\n\n## 2. Reglas\n\nEl TTL del cache respeta la ventana.\n")
    b = tmp_path / "0002-otro.md"
    b.write_text("# Título\n\n## 1. API\n\nOtra cosa sin relación.\n")
    return [a, b]


def test_el_indice_usa_parser_io_con_lineas(tmp_path) -> None:
    index = build_index(_mini_docs(tmp_path))
    seccion = next(s for s in index if "Reglas" in s.section)
    assert seccion.file == "0001-ejemplo.md"
    assert seccion.line_start >= 1 and "TTL" in seccion.body


def test_el_ranking_es_lexico_y_determinista(tmp_path) -> None:
    index = build_index(_mini_docs(tmp_path))
    primera = search(index, "¿dónde define el TTL del cache?")
    segunda = search(index, "¿dónde define el TTL del cache?")
    assert primera == segunda
    assert primera and "Reglas" in primera[0].section


def test_las_citas_inventadas_se_rechazan(tmp_path) -> None:
    index = build_index(_mini_docs(tmp_path))
    card = AnswerCard(
        question="q",
        answer="a",
        citations=[
            Citation(file="0001-ejemplo.md", section="2. Reglas", line_start=3),
            Citation(file="no-existe.md", section="9. Inventada", line_start=1),
        ],
    )
    problemas = citations_valid(card, index)
    assert len(problemas) == 1 and "no-existe.md" in problemas[0]
    assert citations_valid(AnswerCard(question="q", answer="a"), index) == []


def test_el_contexto_muestra_el_formato_de_cita(tmp_path) -> None:
    index = build_index(_mini_docs(tmp_path))
    texto = format_context(index[:1])
    assert "«" in texto and "líneas" in texto


def test_la_pregunta_con_secreto_no_entra_al_prompt(tmp_path) -> None:
    from adr_index.main import ask

    index = build_index(_mini_docs(tmp_path))
    stub = StubProvider()
    try:
        ask(build_client(stub)["client"], index, "¿el token ghp_AbCdEfGhIjKlMnOpQrStUvWxYz123456 está en algún ADR?")
    except RuntimeError:
        pass  # el stub cita un ADR fuera del mini índice: aquí importa el prompt, no la cita
    assert stub.last_request is not None, "el cliente nunca fue llamado"
    mensaje = json.dumps([m["content"] for m in stub.last_request.messages], ensure_ascii=False)
    assert "ghp_" not in mensaje, "el secreto de la pregunta entró al prompt"


def test_el_judge_evoluciona_el_dataset_completo(tmp_path) -> None:
    dataset = EvalDataset.from_jsonl(DEFAULT_DATASET)
    client = build_client(StubProvider())["client"]
    out = run_evalset(dataset, judge_for(client), mode="full", threshold=1.0)
    assert out.total == 3
    assert out.passed == 3
