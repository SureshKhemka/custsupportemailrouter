"""A simulated agent for evals (clearly labelled as simulated in every report).

It works through cases waiting for a human the way a careful agent would, using the labels:
- a draft with every required fact and no forbidden fact is approved unchanged;
- a draft missing required facts is rejected ("incomplete");
- a case without a draft (legal, abuse) gets a short authored holding reply;
- proposed actions the label forbids are skipped (approval without actions = a decision change).
It acts `evals.simulated_agent.minutes_after_arrival` after the email arrived (SLA, BM-5).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mocks.seed import resolve_relative
from router.dataset.loader import LoadedRecord
from router.gate import extract as X
from router.clients import ServiceError
from router.review.service import ReviewError, ReviewService

# Short holding replies an agent would write, in the customer's language (the gate checks the language).
AUTHORED = {
    "en": "Hello{name},\n\nThank you for your message. A member of our team has received it and will reply to you "
          "personally.\n\nWarm regards,\nCustomer Care\n",
    "hi": "नमस्ते{name},\n\nआपके संदेश के लिए धन्यवाद। हमारी टीम ने इसे प्राप्त कर लिया है और जल्द ही आपको व्यक्तिगत रूप से "
          "उत्तर देगी।\n\nसादर,\nग्राहक सेवा\n",
    "es": "Hola{name},\n\nGracias por su mensaje. Un miembro de nuestro equipo lo ha recibido y le responderá "
          "personalmente.\n\nSaludos cordiales,\nAtención al Cliente\n",
    "de": "Hallo{name},\n\nvielen Dank für Ihre Nachricht. Ein Mitglied unseres Teams hat sie erhalten und wird Ihnen "
          "persönlich antworten.\n\nMit freundlichen Grüßen\nKundenservice\n",
    "fr": "Bonjour{name},\n\nMerci pour votre message. Un membre de notre équipe l'a bien reçu et vous répondra "
          "personnellement.\n\nCordialement,\nService client\n",
    "it": "Buongiorno{name},\n\ngrazie per il suo messaggio. Un membro del nostro team l'ha ricevuto e le risponderà "
          "personalmente.\n\nCordiali saluti,\nAssistenza clienti\n",
    "pt": "Olá{name},\n\nobrigado pela sua mensagem. Um membro da nossa equipa recebeu-a e irá responder-lhe "
          "pessoalmente.\n\nCom os melhores cumprimentos,\nApoio ao Cliente\n",
    "zh": "您好{name}，\n\n感谢您的来信。我们的团队已经收到，并会尽快亲自回复您。\n\n此致\n客户服务\n",
}


def act_on_cases(svc: ReviewService, store, lr_by_message: dict[str, LoadedRecord], now_ref: datetime) -> None:
    cfg = svc.cfg
    tz = ZoneInfo(cfg.app.timezone)
    for case in store.open_cases():
        mails = [e for e in store.case_emails(case["case_id"]) if e.outcome in {"new_case", "follow_up"}]
        lr = lr_by_message.get(mails[-1].message_id) if mails else None
        if lr is None:
            continue
        label = lr.record.label
        t = lr.email.received_at + timedelta(minutes=cfg.evals.simulated_agent.minutes_after_arrival)
        try:
            view = svc.view(case["case_id"], t)
        except ServiceError:  # backends down: a real agent would come back later
            continue
        forbidden = set(label.forbidden_actions) - {a.type for a in label.proposed_actions}
        pending = [a for a in view.actions if a["status"] in {"proposed", "failed", "shadow"}]
        skip_actions = any(a["type"] in forbidden for a in pending)
        try:
            if view.draft is None:
                first = view.facts.customer.name.split()[0] if view.facts.customer else ""
                template = AUTHORED.get(label.language, AUTHORED["en"])
                svc.approve(case["case_id"], "sim-agent", t, text=template.format(name=f" {first}" if first else ""),
                            run_actions=not skip_actions)
                continue
            text = view.draft["text"]
            missing = [f for f in label.reply.must_contain if not _present(f, text, tz, now_ref)]
            bad = [f for f in label.reply.must_not_contain if _present(f, text, tz, now_ref)]
            if missing or bad:
                svc.reject(case["case_id"], "sim-agent", f"draft incomplete or wrong: missing {len(missing)}, "
                                                           f"forbidden {len(bad)}", t)
                continue
            svc.approve(case["case_id"], "sim-agent", t, run_actions=not skip_actions)
        except (ReviewError, ServiceError):
            continue


def _present(f, text: str, tz: ZoneInfo, now_ref: datetime) -> bool:
    """Label facts are relative to the dataset's reference time."""
    if f.kind == "amount":
        return any(abs(a - float(f.value)) < 0.01 for a in X.amounts(text))
    if f.kind == "date":
        target = datetime.fromisoformat(resolve_relative(str(f.value), now_ref)).astimezone(tz).date()
        return any(d == target for d, _ in X.dates(text, target.year))
    return str(f.value).lower() in text.lower()
