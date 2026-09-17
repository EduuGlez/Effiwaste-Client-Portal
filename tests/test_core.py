from types import SimpleNamespace

from app.models import AudienceType, RecommendationStatus, UserRole
from app.services.access import can_access_document
from app.services.markdown_service import render_markdown
from app.services.openai_service import _json_output
from app.services.pdf_service import chunk_page
from app.services.recommendation_service import _report_context


def test_hotel_user_only_sees_matching_scopes():
    user = SimpleNamespace(role=UserRole.USER, chain_id="chain-a", hotel_id="hotel-a")
    global_doc = SimpleNamespace(audience_type=AudienceType.GLOBAL, chain_id=None, hotel_id=None)
    chain_doc = SimpleNamespace(audience_type=AudienceType.CHAIN, chain_id="chain-a", hotel_id=None)
    own_hotel_doc = SimpleNamespace(audience_type=AudienceType.HOTEL, chain_id="chain-a", hotel_id="hotel-a")
    other_hotel_doc = SimpleNamespace(audience_type=AudienceType.HOTEL, chain_id="chain-a", hotel_id="hotel-b")

    assert can_access_document(user, global_doc)
    assert can_access_document(user, chain_doc)
    assert can_access_document(user, own_hotel_doc)
    assert not can_access_document(user, other_hotel_doc)


def test_admin_can_access_every_scope():
    admin = SimpleNamespace(role=UserRole.ADMIN, chain_id=None, hotel_id=None)
    document = SimpleNamespace(audience_type=AudienceType.HOTEL, chain_id="x", hotel_id="y")
    assert can_access_document(admin, document)


def test_chunking_respects_size_and_overlap():
    text = " ".join(f"palabra{i}" for i in range(1200))
    chunks = chunk_page(text, max_tokens=120, overlap=20)
    assert len(chunks) > 2
    assert all(0 < token_count <= 120 for _, token_count in chunks)
    assert all(content.strip() for content, _ in chunks)


def test_openai_json_output_accepts_plain_and_fenced_arrays():
    plain = SimpleNamespace(output_text='[{"title":"A"}]')
    fenced = SimpleNamespace(output_text='```json\n[{"title":"B"}]\n```')
    invalid = SimpleNamespace(output_text="sin json")

    assert _json_output(plain) == [{"title": "A"}]
    assert _json_output(fenced) == [{"title": "B"}]
    assert _json_output(invalid) == []


def test_report_context_keeps_page_and_chunk_order():
    chunks = [
        SimpleNamespace(page_number=2, chunk_index=0, content="segundo"),
        SimpleNamespace(page_number=1, chunk_index=1, content="primero-b"),
        SimpleNamespace(page_number=1, chunk_index=0, content="primero-a"),
    ]

    context = _report_context(chunks)

    assert context.index("primero-a") < context.index("primero-b") < context.index("segundo")


def test_recommendation_status_supports_replacement_of_old_proposals():
    assert RecommendationStatus.SUPERSEDED.value == "superseded"


def test_markdown_answers_render_formatting_and_strip_unsafe_html():
    rendered = render_markdown(
        "## Resumen\n\n**Importante** y *énfasis*.\n\n- Uno\n- Dos\n\n"
        "<script>alert('xss')</script>\n\n[Enlace](javascript:alert('xss'))"
    )

    assert "<h2>Resumen</h2>" in rendered
    assert "<strong>Importante</strong>" in rendered
    assert "<em>énfasis</em>" in rendered
    assert "<ul>" in rendered
    assert "<script" not in rendered
    assert "javascript:" not in rendered
