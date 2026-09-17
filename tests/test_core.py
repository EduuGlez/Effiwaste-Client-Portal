from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import AudienceType, RecommendationStatus, UserRole
from app.security import hash_password, password_policy_error, verify_login_password
from app.services.access import can_access_document
from app.services.email_service import (
    SupportAttachment,
    attachment_extension,
    build_support_message,
    validate_support_text,
)
from app.services.markdown_service import render_markdown
from app.services.openai_service import (
    _evaluation_list_adapter,
    _json_output,
    _validated_json_output,
)
from app.services.pdf_service import chunk_page
from app.services.recommendation_service import (
    _extract_measurement,
    _report_context,
    _report_has_comprehensive_waste_coverage,
    _resolve_evaluation,
)
from app.services.storage import document_path
from app.web import redirect_with_message, sanitize_original_filename


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


def test_chunking_rejects_an_invalid_window():
    with pytest.raises(ValueError):
        chunk_page("contenido", max_tokens=100, overlap=100)


def test_openai_json_output_accepts_plain_and_fenced_arrays():
    plain = SimpleNamespace(output_text='[{"title":"A"}]')
    fenced = SimpleNamespace(output_text='```json\n[{"title":"B"}]\n```')
    invalid = SimpleNamespace(output_text="sin json")

    assert _json_output(plain) == [{"title": "A"}]
    assert _json_output(fenced) == [{"title": "B"}]
    assert _json_output(invalid) == []


def test_openai_evaluations_reject_unknown_outcomes_and_invalid_ids():
    invalid_outcome = SimpleNamespace(
        output_text='[{"id":"27dc63ed-e547-4a9f-9a33-1192f00238fd","outcome":"maybe","summary":"x"}]'
    )
    invalid_id = SimpleNamespace(
        output_text='[{"id":"not-a-uuid","outcome":"successful","summary":"x"}]'
    )

    assert _validated_json_output(invalid_outcome, _evaluation_list_adapter) == []
    assert _validated_json_output(invalid_id, _evaluation_list_adapter) == []


def test_openai_evaluation_accepts_the_complete_evidence_contract():
    valid = SimpleNamespace(
        output_text=(
            '[{"id":"27dc63ed-e547-4a9f-9a33-1192f00238fd",'
            '"outcome":"successful","evidence_type":"qualitative_absence",'
            '"report_is_comparable":true,"baseline_evidence":"Sopas: 13,24 %",'
            '"current_evidence":"La clasificación completa ya no incluye sopas",'
            '"summary":"Sopas deja de aparecer en el listado completo."}]'
        )
    )

    result = _validated_json_output(valid, _evaluation_list_adapter)

    assert result[0]["outcome"] == "successful"
    assert result[0]["report_is_comparable"] is True


def test_report_context_keeps_page_and_chunk_order():
    chunks = [
        SimpleNamespace(page_number=2, chunk_index=0, content="segundo"),
        SimpleNamespace(page_number=1, chunk_index=1, content="primero-b"),
        SimpleNamespace(page_number=1, chunk_index=0, content="primero-a"),
    ]

    context = _report_context(chunks)

    assert context.index("primero-a") < context.index("primero-b") < context.index("segundo")


def test_only_a_comprehensive_waste_report_can_prove_improvement_by_absence():
    report = (
        "Informe mensual de desperdicio. Desperdicio total 850 kg/mes. "
        "Desperdicio por categoría de alimentos. Comparación mensual y serie histórica. "
    ) * 30
    recipe = (
        "Receta de salmorejo con tomate, pan, aceite y tiempos de preparación. "
        "Instrucciones de cocina y presentación del plato. "
    ) * 30

    assert _report_has_comprehensive_waste_coverage(report)
    assert not _report_has_comprehensive_waste_coverage(recipe)


def test_absence_is_success_only_with_a_comparable_comprehensive_report():
    evaluation = {
        "outcome": "successful",
        "evidence_type": "qualitative_absence",
        "report_is_comparable": True,
        "baseline_evidence": "Sopas representaban el 13,24 % del desperdicio.",
        "current_evidence": "El listado completo del nuevo mes ya no contiene sopas.",
        "summary": "Sopas deja de aparecer en el análisis completo por categorías.",
    }

    assert _resolve_evaluation(evaluation, True)[0] == "successful"
    assert _resolve_evaluation(evaluation, False)[0] == "inconclusive"


def test_quantitative_success_requires_a_real_reduction_in_the_same_unit():
    base = {
        "outcome": "successful",
        "evidence_type": "quantitative",
        "report_is_comparable": True,
        "baseline_evidence": "1015 kg de desperdicio total.",
        "current_evidence": "842 kg de desperdicio total.",
        "summary": "El desperdicio total baja 173 kg.",
    }
    not_reduced = {**base, "current_evidence": "1.100 kg de desperdicio total."}

    assert _resolve_evaluation(base, True)[0] == "successful"
    assert _resolve_evaluation(not_reduced, True)[0] == "inconclusive"
    assert _extract_measurement("1.015 kg") == ("mass", 1_015_000.0)
    assert _extract_measurement("9,39 %") == ("percentage", 9.39)


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


def test_password_policy_and_login_verification_fail_closed():
    assert password_policy_error("short") is not None
    assert password_policy_error("onlylowercasepassword") is not None
    assert password_policy_error("Correcta-2026!") is None

    encoded = hash_password("Correcta-2026!")
    assert verify_login_password("Correcta-2026!", encoded)
    assert not verify_login_password("incorrecta", encoded)
    assert not verify_login_password("incorrecta", None)
    assert not verify_login_password("incorrecta", "hash-corrupto")


def test_production_configuration_rejects_development_defaults():
    with pytest.raises(ValidationError, match="Configuración insegura"):
        Settings(_env_file=None, environment="production")


def test_production_configuration_accepts_required_controls():
    configuration = Settings(
        _env_file=None,
        environment="production",
        session_secret="a-secure-random-secret-with-more-than-32-characters",
        cookie_secure=True,
        seed_demo_data=False,
        admin_password="Admin-Seguro-2026!",
        database_url="postgresql+psycopg://suite:secret@db:5432/suite",
        openai_api_key="sk-test-only-not-a-real-key",
    )

    assert configuration.is_production
    assert configuration.cookie_secure


def test_storage_names_and_display_names_cannot_escape_the_upload_directory():
    safe_path = document_path("27dc63ed-e547-4a9f-9a33-1192f00238fd.pdf")
    assert safe_path.name == "27dc63ed-e547-4a9f-9a33-1192f00238fd.pdf"
    with pytest.raises(ValueError):
        document_path("../../etc/passwd.pdf")

    assert sanitize_original_filename("C:\\fakepath\\manual\x00.pdf") == "manual.pdf"
    assert sanitize_original_filename("../../informe.pdf") == "informe.pdf"


def test_redirects_only_accept_internal_absolute_paths():
    assert redirect_with_message("/login", error="No válido").headers["location"].startswith(
        "/login?error="
    )
    with pytest.raises(ValueError):
        redirect_with_message("//attacker.example")


def test_support_email_uses_server_identity_and_keeps_attachments():
    settings = Settings(
        _env_file=None,
        support_email="soporte@example.com",
        smtp_from_email="portal@example.com",
    )
    message = build_support_message(
        settings=settings,
        user_name="Ana Hotelera",
        user_email="ana@example.com",
        hotel_name="Hotel Atlántico",
        chain_name="Costa Hotels",
        subject="Problema con un informe",
        body="No puedo abrir el informe del mes de agosto.",
        attachments=[SupportAttachment("captura.png", b"imagen")],
    )

    assert message["To"] == "soporte@example.com"
    assert message["Reply-To"] == "ana@example.com"
    assert "Hotel Atlántico" in message.get_body(preferencelist=("plain",)).get_content()
    assert next(message.iter_attachments()).get_filename() == "captura.png"


def test_support_request_rejects_short_text_and_unsafe_extensions():
    with pytest.raises(ValueError, match="asunto"):
        validate_support_text("No", "Este mensaje sí tiene detalle suficiente")
    with pytest.raises(ValueError, match="Formato"):
        attachment_extension("programa.exe")
    assert attachment_extension("captura.JPG") == ".jpg"
