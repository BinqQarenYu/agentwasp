"""Response Validation Layer — hard enforcement before any message is sent.

Runs AFTER the LLM loop completes, BEFORE the response reaches Telegram or UI.
This is NOT prompt-based — it uses deterministic pattern matching against the
execution trace to catch four failure modes:

  grounding_fail  response contains data/prices that no skill retrieved
  incomplete      response claims an action was completed but the skill never ran
  drift           response is about a different topic than what was requested
  ok              response passed all checks

Architecture:
- ResponseValidator.validate() is called once per turn, after cleanup
- Failed validations trigger one correction LLM round (no new skill execution)
  so the agent admits what it couldn't do rather than sending a false claim
- If correction also fails or is unavailable, a safe fallback_response is used
- All blocks are logged at WARNING level for observability
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..utils.lang_detect import detect_lang


# ── Localized user-facing strings ─────────────────────────────────────────────
# All fallback_response and correction_hint strings use this table.
# Add a new key per language as needed. "en" is the authoritative default.

_L: dict[str, dict[str, str]] = {
    "en": {
        "generic_fail":        "I couldn't complete that request correctly. Want me to try again?",
        "grounding_fail":      "I couldn't retrieve that information in real time. Want me to try again?",
        "grounding_hint":      (
            "[VALIDATION BLOCK] Your response contains prices or specific data that you did not "
            "retrieve from any skill — that is a hallucination. Do not invent data. "
            "Respond honestly: 'I could not obtain that information in real time. Shall I try?'"
        ),
        "email_fail":          "The email could not be sent in this execution. Want me to try again?",
        "email_hint":          (
            "[VALIDATION BLOCK] Your response says an email was sent, "
            "but gmail() was never called in this execution. "
            "Do not claim to have sent something you did not send. "
            "Respond honestly about what you could and could not complete."
        ),
        "task_fail":           "The scheduled task was not registered. Want me to try again?",
        "task_hint":           (
            "[VALIDATION BLOCK] Your response says the task was scheduled, "
            "but task_manager() was never called. "
            "Do not claim to have scheduled something you did not schedule. "
            "Respond honestly about what you could complete."
        ),
        "narrative_fail":      "I couldn't complete that action right now. Want me to try again?",
        "future_send_fail":    "I couldn't send the email in this execution. Want me to try again?",
        "future_send_hint":    (
            "[VALIDATION BLOCK] Your response says you will send or are sending an email, "
            "but gmail() with action='send' was never executed in this session. "
            "Do not promise actions that did not happen. "
            "Respond honestly: 'I could not send the email in this execution.'"
        ),
        "future_cap_fail":     "I couldn't take the screenshot in this execution. Want me to try again?",
        "future_proc_fail":    "I couldn't complete that action right now. Want me to try again?",
        "placeholder_fail":    "I couldn't complete the report correctly — some data was not available. I'll try again.",
        "placeholder_hint":    (
            "[VALIDATION BLOCK] Your response contains incomplete or placeholder values: {detail}. "
            "The real data did not reach the report. "
            "Honestly indicate that the data was not available instead of showing empty values."
        ),
        "multipart_short_fail": "I need more information to answer all your questions correctly. Can you repeat them one by one?",
        "multipart_short_hint": (
            "[VALIDATION BLOCK] The user asked {n} distinct questions "
            "but your response is too short ({chars} characters) to cover all of them. "
            "Answer ALL parts: number each section (1., 2., etc.) and answer them one by one. "
            "If you cannot answer a part, say so explicitly for that part."
        ),
        "multipart_struct_fail": "I couldn't structure a complete response for all your questions. Can you ask them one at a time?",
        "multipart_struct_hint": (
            "[VALIDATION BLOCK] The user asked {n} questions or distinct sections. "
            "Your response does not have enough section markers to cover all of them. "
            "Structure your response with numbered sections or headers. "
            "Each question/section from the user must have its own part in the response."
        ),
        "drift_fail":          "I couldn't align the response with what you asked. You can ask me to try again.",
        "drift_browser_hint":  (
            "[VALIDATION BLOCK] The user asked for screenshots/navigation, "
            "but your response contains cryptocurrency prices that nobody asked for. "
            "Do NOT mix in unsolicited data from another domain. "
            "If you could not take the screenshot, say so honestly without inventing data: "
            "'I could not access [URL]. Want me to try again?'"
        ),
        "drift_browser_fail":  "I couldn't complete the requested capture. Want me to try again?",
        "context_contam_fail": (
            "I understand you changed topic. I'll focus on the new request.\n\n"
            "Can you repeat your question so I can answer correctly?"
        ),
        "context_contam_hint": (
            "[CONTEXT RESET VIOLATION] The user changed topic: they are now asking about "
            "'{new}', not '{old}'. Your response still references the previous context ({old}). "
            "COMPLETELY IGNORE the previous topic. "
            "Respond ONLY to what the user just asked, as if it were the first message."
        ),
        "planning_fail":       (
            "Understood — here is the plan without executing anything:\n\n"
            "I could not generate the full plan. "
            "Can you repeat your request indicating 'plan only'?"
        ),
        "planning_hint":       (
            "[PLANNING MODE VIOLATION] The user asked for ONLY the plan, without executing anything. "
            "Violation detected: {detail}. "
            "Do NOT execute anything. Respond ONLY with:\n"
            "1. OVERVIEW — description of the approach\n"
            "2. STEPS — numbered list of what would be done and with which tools\n"
            "3. ARCHITECTURE — how the parts connect\n"
            "4. Close with: \"When you want me to execute, say: run it / go ahead.\""
        ),
        "track_false_positive_fail": (
            "I could not verify the package tracking information — no browser search was executed. "
            "Want me to actually check it now?"
        ),
        "track_false_positive_hint": (
            "[VALIDATION BLOCK] Your response claims specific package tracking status, "
            "but no browser() skill was executed to retrieve that data. "
            "This is a hallucination — you cannot know the package status without checking the website. "
            "Respond honestly: 'I was unable to check the package status. "
            "Use browser(action=\"track\", tracking_number=\"...\") to check it. Want me to try?'"
        ),
    },
    "es": {
        "generic_fail":        "No pude completar esa solicitud correctamente. ¿Quieres que lo intente de nuevo?",
        "grounding_fail":      "No pude obtener esa información en tiempo real. ¿Quieres que lo intente de nuevo?",
        "grounding_hint":      (
            "[VALIDATION BLOCK] Tu respuesta contiene precios o datos específicos "
            "que no obtuviste de ninguna skill — eso es una alucinación. "
            "No inventas datos. Responde honestamente: "
            "'No pude obtener esa información en tiempo real. ¿Quieres que lo intente?'"
        ),
        "email_fail":          "No se pudo enviar el correo en esta ejecución. ¿Quieres que lo intente de nuevo?",
        "email_hint":          (
            "[VALIDATION BLOCK] Tu respuesta dice que enviaste un email, "
            "pero gmail() nunca fue llamado en esta ejecución. "
            "No afirmes haber enviado algo que no enviaste. "
            "Responde honestamente qué pudiste completar y qué no."
        ),
        "task_fail":           "No se registró la tarea programada. ¿Quieres que lo intente de nuevo?",
        "task_hint":           (
            "[VALIDATION BLOCK] Tu respuesta dice que la tarea quedó programada, "
            "pero task_manager() nunca fue llamado. "
            "No afirmes haber programado algo que no programaste. "
            "Responde honestamente qué pudiste completar."
        ),
        "narrative_fail":      "No pude completar esa acción en este momento. ¿Quieres que lo intente de nuevo?",
        "future_send_fail":    "No pude enviar el correo en esta ejecución. ¿Quieres que lo intente de nuevo?",
        "future_send_hint":    (
            "[VALIDATION BLOCK] Tu respuesta dice que vas a enviar o enviarás un correo, "
            "pero gmail() con action='send' nunca fue ejecutado en esta sesión. "
            "No prometas acciones que no ocurrieron. "
            "Responde honestamente: 'No pude enviar el correo en esta ejecución.'"
        ),
        "future_cap_fail":     "No pude tomar la captura de pantalla en esta ejecución. ¿Quieres que lo intente de nuevo?",
        "future_proc_fail":    "No pude completar esa acción en este momento. ¿Quieres que lo intente de nuevo?",
        "placeholder_fail":    "No pude completar el informe correctamente — algunos datos no estaban disponibles. Intentaré nuevamente.",
        "placeholder_hint":    (
            "[VALIDATION BLOCK] Tu respuesta contiene valores incompletos o de placeholder: {detail}. "
            "Los datos reales no llegaron al reporte. "
            "Indica honestamente que los datos no estaban disponibles en lugar de mostrar valores vacíos."
        ),
        "multipart_short_fail": "Necesito más información para responder todas tus preguntas correctamente. ¿Puedes repetirlas una por una?",
        "multipart_short_hint": (
            "[VALIDATION BLOCK] El usuario hizo {n} preguntas distintas "
            "pero tu respuesta es demasiado corta ({chars} caracteres) para cubrirlas todas. "
            "Responde TODAS las partes: numera cada sección (1., 2., etc.) y respóndelas una por una. "
            "Si no puedes responder alguna parte, dilo explícitamente para esa parte."
        ),
        "multipart_struct_fail": "No pude estructurar una respuesta completa para todas tus preguntas. ¿Puedes hacerlas una a la vez?",
        "multipart_struct_hint": (
            "[VALIDATION BLOCK] El usuario hizo {n} preguntas o secciones distintas. "
            "Tu respuesta no tiene marcadores de sección suficientes para cubrirlas todas. "
            "Estructura tu respuesta con secciones numeradas o encabezados. "
            "Cada pregunta / sección del usuario debe tener su propio apartado en la respuesta."
        ),
        "drift_fail":          "No logré alinear la respuesta con lo que pediste. Puedes pedirme que lo intente de nuevo.",
        "drift_browser_hint":  (
            "[VALIDATION BLOCK] El usuario pidió capturas de pantalla/navegación, "
            "pero tu respuesta contiene precios de criptomonedas que nadie pidió. "
            "NO mezcles datos no solicitados. "
            "Si no pudiste tomar la captura, dilo honestamente sin inventar datos de otro tema: "
            "'No pude acceder a [URL]. ¿Quieres que lo intente de nuevo?'"
        ),
        "drift_browser_fail":  "No pude completar la captura solicitada. ¿Quieres que lo intente de nuevo?",
        "context_contam_fail": (
            "Entiendo que cambiaste de tema. Me enfoco en la nueva solicitud.\n\n"
            "¿Puedes repetir tu pregunta para que te responda correctamente?"
        ),
        "context_contam_hint": (
            "[CONTEXT RESET VIOLATION] El usuario cambió de tema: ahora pregunta sobre "
            "'{new}', no sobre '{old}'. Tu respuesta aún hace referencia al contexto anterior ({old}). "
            "IGNORA completamente el tema anterior. "
            "Responde ÚNICAMENTE a lo que el usuario acaba de preguntar ahora, "
            "como si fuera la primera vez que habla contigo."
        ),
        "planning_fail":       (
            "Entendido — aquí está el plan sin ejecutar nada:\n\n"
            "No pude generar el plan completo. "
            "¿Puedes repetir tu solicitud indicando 'solo el plan'?"
        ),
        "planning_hint":       (
            "[PLANNING MODE VIOLATION] El usuario pidió SOLO el plan, sin ejecutar nada. "
            "Violación detectada: {detail}. "
            "NO ejecutes nada. Responde ÚNICAMENTE con:\n"
            "1. OVERVIEW — descripción del enfoque\n"
            "2. PASOS — lista numerada de lo que se haría y con qué herramientas\n"
            "3. ARQUITECTURA — cómo se conectan las partes\n"
            "4. Cierra con: \"Cuando quieras que ejecute, dime: ejecuta / ponlo en marcha.\""
        ),
        "track_false_positive_fail": (
            "No pude verificar el estado del paquete — no se ejecutó ninguna búsqueda en el navegador. "
            "¿Quieres que lo busque ahora?"
        ),
        "track_false_positive_hint": (
            "[VALIDATION BLOCK] Tu respuesta afirma el estado específico de un paquete, "
            "pero no se ejecutó ninguna skill browser() para recuperar esos datos. "
            "Esto es una alucinación — no puedes saber el estado del paquete sin consultarlo. "
            "Responde honestamente: 'No pude verificar el estado del paquete. "
            "¿Quieres que lo rastree ahora con browser(action=\"track\", tracking_number=\"...\")?"
        ),
    },
    "pt": {
        "generic_fail":        "Não consegui completar esse pedido corretamente. Quer que tente novamente?",
        "grounding_fail":      "Não consegui obter essa informação em tempo real. Quer que tente novamente?",
        "grounding_hint":      (
            "[VALIDATION BLOCK] Sua resposta contém preços ou dados específicos "
            "que não foram obtidos de nenhuma skill — isso é uma alucinação. "
            "Responda honestamente: 'Não consegui obter essa informação em tempo real. Tentar novamente?'"
        ),
        "email_fail":          "Não foi possível enviar o e-mail nesta execução. Quer que tente novamente?",
        "email_hint":          (
            "[VALIDATION BLOCK] Sua resposta diz que um e-mail foi enviado, "
            "mas gmail() nunca foi chamado nesta execução. "
            "Não afirme ter enviado algo que não enviou."
        ),
        "task_fail":           "A tarefa agendada não foi registrada. Quer que tente novamente?",
        "task_hint":           (
            "[VALIDATION BLOCK] Sua resposta diz que a tarefa foi agendada, "
            "mas task_manager() nunca foi chamado. Responda honestamente."
        ),
        "narrative_fail":      "Não consegui completar essa ação agora. Quer que tente novamente?",
        "future_send_fail":    "Não consegui enviar o e-mail nesta execução. Quer que tente novamente?",
        "future_send_hint":    "[VALIDATION BLOCK] Não prometa enviar e-mails que não foram enviados.",
        "future_cap_fail":     "Não consegui tirar a captura de tela. Quer que tente novamente?",
        "future_proc_fail":    "Não consegui completar essa ação agora. Quer que tente novamente?",
        "placeholder_fail":    "Não consegui completar o relatório — alguns dados não estavam disponíveis.",
        "placeholder_hint":    "[VALIDATION BLOCK] Sua resposta contém valores de placeholder: {detail}.",
        "multipart_short_fail": "Pode repetir suas perguntas uma por uma?",
        "multipart_short_hint": "[VALIDATION BLOCK] O usuário fez {n} perguntas mas a resposta é curta ({chars} chars).",
        "multipart_struct_fail": "Não consegui estruturar uma resposta completa. Pode perguntar uma de cada vez?",
        "multipart_struct_hint": "[VALIDATION BLOCK] Estruture a resposta com seções numeradas para {n} partes.",
        "drift_fail":          "Não consegui alinhar a resposta com o que foi pedido.",
        "drift_browser_hint":  "[VALIDATION BLOCK] O usuário pediu capturas, não preços de cripto. Seja honesto sobre falhas.",
        "drift_browser_fail":  "Não consegui completar a captura solicitada. Quer que tente novamente?",
        "context_contam_fail": "Entendido, foco no novo assunto. Pode repetir a pergunta?",
        "context_contam_hint": "[CONTEXT RESET VIOLATION] Usuário mudou de '{old}' para '{new}'. Ignore o contexto anterior.",
        "planning_fail":       "Não consegui gerar o plano completo. Pode repetir indicando 'só o plano'?",
        "planning_hint":       "[PLANNING MODE VIOLATION] Apenas o plano foi solicitado. Violação: {detail}.",
    },
    "fr": {
        "generic_fail":        "Je n'ai pas pu compléter cette demande correctement. Voulez-vous que je réessaie?",
        "grounding_fail":      "Je n'ai pas pu obtenir cette information en temps réel. Voulez-vous que je réessaie?",
        "grounding_hint":      (
            "[VALIDATION BLOCK] Votre réponse contient des prix ou données spécifiques "
            "non récupérés par aucune skill — c'est une hallucination. "
            "Répondez honnêtement: 'Je n'ai pas pu obtenir cette information en temps réel.'"
        ),
        "email_fail":          "L'e-mail n'a pas pu être envoyé. Voulez-vous que je réessaie?",
        "email_hint":          "[VALIDATION BLOCK] Ne prétendez pas avoir envoyé un e-mail si gmail() n'a pas été appelé.",
        "task_fail":           "La tâche planifiée n'a pas été enregistrée. Voulez-vous réessayer?",
        "task_hint":           "[VALIDATION BLOCK] Ne prétendez pas avoir planifié quelque chose si task_manager() n'a pas été appelé.",
        "narrative_fail":      "Je n'ai pas pu effectuer cette action maintenant. Voulez-vous réessayer?",
        "future_send_fail":    "Je n'ai pas pu envoyer l'e-mail. Voulez-vous réessayer?",
        "future_send_hint":    "[VALIDATION BLOCK] Ne promettez pas d'envoyer des e-mails qui n'ont pas été envoyés.",
        "future_cap_fail":     "Je n'ai pas pu prendre la capture d'écran. Voulez-vous réessayer?",
        "future_proc_fail":    "Je n'ai pas pu compléter cette action maintenant. Voulez-vous réessayer?",
        "placeholder_fail":    "Je n'ai pas pu compléter le rapport — certaines données n'étaient pas disponibles.",
        "placeholder_hint":    "[VALIDATION BLOCK] Votre réponse contient des valeurs placeholder: {detail}.",
        "multipart_short_fail": "Pouvez-vous répéter vos questions une par une?",
        "multipart_short_hint": "[VALIDATION BLOCK] L'utilisateur a posé {n} questions mais la réponse est trop courte ({chars} chars).",
        "multipart_struct_fail": "Pouvez-vous poser vos questions une par une?",
        "multipart_struct_hint": "[VALIDATION BLOCK] Structurez la réponse avec des sections numérotées pour {n} parties.",
        "drift_fail":          "Je n'ai pas pu aligner la réponse avec ce qui était demandé.",
        "drift_browser_hint":  "[VALIDATION BLOCK] L'utilisateur a demandé des captures, pas des prix crypto. Soyez honnête sur les échecs.",
        "drift_browser_fail":  "Je n'ai pas pu compléter la capture demandée. Voulez-vous réessayer?",
        "context_contam_fail": "Je comprends que vous avez changé de sujet. Pouvez-vous répéter votre question?",
        "context_contam_hint": "[CONTEXT RESET VIOLATION] L'utilisateur est passé de '{old}' à '{new}'. Ignorez le contexte précédent.",
        "planning_fail":       "Je n'ai pas pu générer le plan complet. Pouvez-vous répéter en indiquant 'plan seulement'?",
        "planning_hint":       "[PLANNING MODE VIOLATION] Seulement le plan a été demandé. Violation: {detail}.",
    },
    "de": {
        "generic_fail":        "Ich konnte diese Anfrage nicht korrekt abschließen. Soll ich es erneut versuchen?",
        "grounding_fail":      "Ich konnte diese Information nicht in Echtzeit abrufen. Soll ich es erneut versuchen?",
        "email_fail":          "Die E-Mail konnte nicht gesendet werden. Soll ich es erneut versuchen?",
        "task_fail":           "Die geplante Aufgabe wurde nicht registriert. Soll ich es erneut versuchen?",
        "narrative_fail":      "Ich konnte diese Aktion jetzt nicht ausführen. Soll ich es erneut versuchen?",
        "future_send_fail":    "Ich konnte die E-Mail nicht senden. Soll ich es erneut versuchen?",
        "future_cap_fail":     "Ich konnte den Screenshot nicht aufnehmen. Soll ich es erneut versuchen?",
        "future_proc_fail":    "Ich konnte diese Aktion jetzt nicht ausführen. Soll ich es erneut versuchen?",
        "placeholder_fail":    "Ich konnte den Bericht nicht korrekt erstellen — einige Daten waren nicht verfügbar.",
        "multipart_short_fail": "Können Sie Ihre Fragen einzeln wiederholen?",
        "multipart_struct_fail": "Können Sie Ihre Fragen einzeln stellen?",
        "drift_fail":          "Ich konnte die Antwort nicht an Ihre Anfrage anpassen.",
        "drift_browser_fail":  "Ich konnte die angeforderte Aufnahme nicht abschließen. Soll ich es erneut versuchen?",
        "context_contam_fail": "Ich verstehe, dass Sie das Thema gewechselt haben. Können Sie Ihre Frage wiederholen?",
        "planning_fail":       "Ich konnte den vollständigen Plan nicht erstellen. Können Sie mit 'nur der Plan' wiederholen?",
    },
    "zh": {
        "generic_fail":        "我无法正确完成该请求。要我重试吗？",
        "grounding_fail":      "我无法实时获取该信息。要我重试吗？",
        "email_fail":          "此次执行中无法发送电子邮件。要我重试吗？",
        "task_fail":           "计划任务未能注册。要我重试吗？",
        "narrative_fail":      "我现在无法完成该操作。要我重试吗？",
        "future_send_fail":    "此次执行中无法发送电子邮件。要我重试吗？",
        "future_cap_fail":     "此次执行中无法截图。要我重试吗？",
        "future_proc_fail":    "我现在无法完成该操作。要我重试吗？",
        "placeholder_fail":    "无法正确生成报告——部分数据不可用。",
        "multipart_short_fail": "能否逐一重复您的问题？",
        "multipart_struct_fail": "能否逐一提问？",
        "drift_fail":          "无法将回复与您的请求对齐。",
        "drift_browser_fail":  "无法完成所请求的截图。要我重试吗？",
        "context_contam_fail": "我理解您换了话题，请重复您的问题。",
        "planning_fail":       "无法生成完整计划。能否注明\u300c仅计划\u300d后重新提问？",
    },
    "ja": {
        "generic_fail":        "リクエストを正しく完了できませんでした。もう一度試しますか？",
        "grounding_fail":      "リアルタイムでその情報を取得できませんでした。もう一度試しますか？",
        "email_fail":          "この実行ではメールを送信できませんでした。もう一度試しますか？",
        "task_fail":           "スケジュールタスクが登録されませんでした。もう一度試しますか？",
        "narrative_fail":      "今はそのアクションを完了できませんでした。もう一度試しますか？",
        "future_send_fail":    "この実行ではメールを送信できませんでした。もう一度試しますか？",
        "future_cap_fail":     "この実行ではスクリーンショットを撮影できませんでした。もう一度試しますか？",
        "future_proc_fail":    "今はそのアクションを完了できませんでした。もう一度試しますか？",
        "placeholder_fail":    "レポートを正しく完成できませんでした——一部のデータが利用できませんでした。",
        "multipart_short_fail": "質問を一つずつ繰り返していただけますか？",
        "multipart_struct_fail": "質問を一つずつお願いできますか？",
        "drift_fail":          "回答をリクエストに合わせることができませんでした。",
        "drift_browser_fail":  "要求されたキャプチャを完了できませんでした。もう一度試しますか？",
        "context_contam_fail": "話題が変わったことは理解しました。質問を繰り返していただけますか？",
        "planning_fail":       "完全なプランを生成できませんでした。「プランのみ」と明記して再度お願いできますか？",
    },
    "ko": {
        "generic_fail":        "요청을 올바르게 완료할 수 없었습니다. 다시 시도할까요?",
        "grounding_fail":      "실시간으로 정보를 가져올 수 없었습니다. 다시 시도할까요?",
        "email_fail":          "이번 실행에서 이메일을 보낼 수 없었습니다. 다시 시도할까요?",
        "task_fail":           "예약된 작업이 등록되지 않았습니다. 다시 시도할까요?",
        "narrative_fail":      "지금은 그 작업을 완료할 수 없었습니다. 다시 시도할까요?",
        "future_send_fail":    "이번 실행에서 이메일을 보낼 수 없었습니다. 다시 시도할까요?",
        "future_cap_fail":     "이번 실행에서 스크린샷을 찍을 수 없었습니다. 다시 시도할까요?",
        "future_proc_fail":    "지금은 그 작업을 완료할 수 없었습니다. 다시 시도할까요?",
        "placeholder_fail":    "보고서를 올바르게 완성할 수 없었습니다—일부 데이터를 사용할 수 없었습니다.",
        "multipart_short_fail": "질문을 하나씩 반복해 주시겠어요?",
        "multipart_struct_fail": "질문을 하나씩 해 주시겠어요?",
        "drift_fail":          "응답을 요청에 맞게 조정할 수 없었습니다.",
        "drift_browser_fail":  "요청한 캡처를 완료할 수 없었습니다. 다시 시도할까요?",
        "context_contam_fail": "주제가 바뀐 것을 이해했습니다. 질문을 반복해 주시겠어요?",
        "planning_fail":       "전체 계획을 생성할 수 없었습니다. '계획만'을 명시하여 다시 요청해 주시겠어요?",
    },
    "ar": {
        "generic_fail":        "لم أتمكن من إكمال هذا الطلب بشكل صحيح. هل تريد أن أحاول مرة أخرى؟",
        "grounding_fail":      "لم أتمكن من الحصول على هذه المعلومات في الوقت الفعلي. هل تريد أن أحاول مرة أخرى؟",
        "email_fail":          "لم يتم إرسال البريد الإلكتروني في هذه المرة. هل تريد أن أحاول مرة أخرى؟",
        "task_fail":           "لم يتم تسجيل المهمة المجدولة. هل تريد أن أحاول مرة أخرى؟",
        "narrative_fail":      "لم أتمكن من إتمام هذا الإجراء الآن. هل تريد أن أحاول مرة أخرى؟",
        "future_send_fail":    "لم أتمكن من إرسال البريد الإلكتروني. هل تريد أن أحاول مرة أخرى؟",
        "future_cap_fail":     "لم أتمكن من التقاط لقطة الشاشة. هل تريد أن أحاول مرة أخرى؟",
        "future_proc_fail":    "لم أتمكن من إتمام هذا الإجراء الآن. هل تريد أن أحاول مرة أخرى؟",
        "placeholder_fail":    "لم أتمكن من إكمال التقرير بشكل صحيح — بعض البيانات غير متوفرة.",
        "multipart_short_fail": "هل يمكنك تكرار أسئلتك واحداً تلو الآخر؟",
        "multipart_struct_fail": "هل يمكنك طرح الأسئلة واحداً في كل مرة؟",
        "drift_fail":          "لم أتمكن من مواءمة الإجابة مع ما طلبته.",
        "drift_browser_fail":  "لم أتمكن من إكمال التقاط الصورة المطلوبة. هل تريد أن أحاول مرة أخرى؟",
        "context_contam_fail": "أفهم أنك غيّرت الموضوع. هل يمكنك تكرار سؤالك؟",
        "planning_fail":       "لم أتمكن من إنشاء الخطة الكاملة. هل يمكنك تحديد 'الخطة فقط' وإعادة الطلب؟",
    },
    "ru": {
        "generic_fail":        "Не удалось корректно выполнить запрос. Попробовать снова?",
        "grounding_fail":      "Не удалось получить информацию в реальном времени. Попробовать снова?",
        "email_fail":          "Письмо не было отправлено. Попробовать снова?",
        "task_fail":           "Запланированная задача не была зарегистрирована. Попробовать снова?",
        "narrative_fail":      "Не удалось выполнить это действие сейчас. Попробовать снова?",
        "future_send_fail":    "Не удалось отправить письмо. Попробовать снова?",
        "future_cap_fail":     "Не удалось сделать скриншот. Попробовать снова?",
        "future_proc_fail":    "Не удалось выполнить это действие сейчас. Попробовать снова?",
        "placeholder_fail":    "Не удалось корректно создать отчёт — некоторые данные недоступны.",
        "multipart_short_fail": "Не могли бы вы повторить вопросы по одному?",
        "multipart_struct_fail": "Не могли бы вы задавать вопросы по одному?",
        "drift_fail":          "Не удалось выровнять ответ с вашим запросом.",
        "drift_browser_fail":  "Не удалось завершить запрошенный захват экрана. Попробовать снова?",
        "context_contam_fail": "Понял, что вы сменили тему. Не могли бы вы повторить вопрос?",
        "planning_fail":       "Не удалось создать полный план. Уточните 'только план' и повторите запрос?",
    },
}


def _t(lang: str, key: str, **fmt) -> str:
    """Get localized string, falling back to English."""
    strings = _L.get(lang) or _L["en"]
    text = strings.get(key) or _L["en"].get(key, "")
    return text.format(**fmt) if fmt else text


@dataclass
class ValidationResult:
    valid: bool
    reason: str = "ok"          # "ok" | "grounding_fail" | "incomplete" | "drift"
    should_retry: bool = False  # inject correction_hint for one honest-response LLM round
    correction_hint: str = ""   # user-role message for the correction round
    fallback_response: str = (
        "I couldn't complete that request correctly. Want me to try again?"
    )


class ResponseValidator:
    """Hard response validation — deterministic, zero LLM calls, zero false positives goal.

    Usage:
        validator = ResponseValidator()
        result = validator.validate(
            response_text=response_text,
            user_input=user_input,
            executed_skills=executed_skills,  # set[str]
            has_any_skill_data=bool,
        )
        if not result.valid:
            # use result.fallback_response or run correction round
    """

    # ── Domain keyword maps (for drift detection) ─────────────────────────────

    _DOMAIN_CRYPTO = re.compile(
        r"\b(?:btc|bitcoin|eth|ethereum|sol|solana|bnb|ada|xrp|doge|cripto|crypto|"
        r"cotizaci[oó]n|binance|coinbase|coingecko|usdt|token|blockchain|defi|"
        r"precio\s+(?:de(?:l?)\s+)?(?:btc|eth|bitcoin|ethereum|crypto|cripto))\b",
        re.IGNORECASE,
    )
    _DOMAIN_WEATHER = re.compile(
        r"\b(?:clima|weather|temperatura|temperature|lluvia|rain|soleado|sunny|"
        r"nublado|cloudy|pron[oó]stico|forecast|viento|wind|hum[eé]dad|humidity|"
        r"tormenta|storm|grados|degrees|celsius|fahrenheit)\b",
        re.IGNORECASE,
    )
    _DOMAIN_SHOPPING = re.compile(
        r"\b(?:zapato|shoe|ropa|clothing|tienda|store|marca|brand|talla|size|"
        r"samsung|iphone|laptop|notebook|televisor|producto|falabella|ripley|"
        r"mercado\s*libre|amazon|aliexpress)\b",
        re.IGNORECASE,
    )

    # ── Real-time data claim patterns ─────────────────────────────────────────

    # Matches price claims: "$94,000", "cotiza a $X", "bajó un 2%", "está en $X"
    _PRICE_CLAIM = re.compile(
        r"(?:"
        r"\$[\d,]+(?:\.\d+)?\b|"                                   # $94,000 or $94.5k
        r"(?:est[aá]|cotiza|vale|cuesta)\s+(?:en\s+)?\$?\d|"       # está en $X
        r"\d+(?:[.,]\d+)?\s*(?:USD|USDT|EUR|BTC|ETH)\b|"           # 94000 USD
        r"(?:subi[oó]|baj[oó]|ca[iy][oó]|aument[oó]|disminuy[oó])\s+(?:un\s+)?\d+(?:[.,]\d+)?%"  # subió un 2%
        r")",
        re.IGNORECASE,
    )

    # ── Completion claim patterns ─────────────────────────────────────────────

    _EMAIL_CLAIM = re.compile(
        r"\b(?:"
        r"envi[eé]\s+(?:el\s+)?(?:correo|email|informe|reporte|resumen)|"
        r"(?:correo|email|informe|reporte)\s+(?:enviado|sent|mandado)|"
        r"te\s+(?:mand[eé]|envi[eé])|"
        r"sent\s+(?:the\s+)?(?:email|report|summary)"
        r")\b",
        re.IGNORECASE,
    )

    _TASK_CLAIM = re.compile(
        r"\b(?:"
        r"(?:tarea|task)\s+(?:programada|creada|scheduled|created|registrada)|"
        r"qued[oó]\s+programad[oa]|"
        r"ya\s+(?:est[aá]|qued[oó])\s+programad[oa]|"
        r"voy\s+a\s+(?:monitorear|revisar|ejecutar|revisar)\s+cada\b"
        r")\b",
        re.IGNORECASE,
    )

    # ── Real-time info request detection ─────────────────────────────────────

    _REALTIME_REQUEST = re.compile(
        r"\b(?:"
        r"precio|price|cu[aá]nto\s+vale|cu[aá]nto\s+cuesta|how\s+much\s+(?:is|does)|"
        r"cotizaci[oó]n|monitorea|rastrea|track|dame\s+el\s+precio|"
        r"qu[eé]\s+precio|valor\s+actual|current\s+(?:price|value)|"
        r"cu[aá]nto\s+est[aá]|what.s\s+the\s+price"
        r")\b",
        re.IGNORECASE,
    )

    _EMAIL_REQUEST = re.compile(
        r"\b(?:env[ií]a(?:me)?|send|manda(?:me)?|mail|email|correo|por\s+correo)\b",
        re.IGNORECASE,
    )

    _SCHEDULE_REQUEST = re.compile(
        r"\b(?:programa(?:r)?|schedule|agenda(?:r)?|cada\s+\d|every\s+\d|"
        r"recurrente|recurring|monitorea\s+cada|automatiza)\b",
        re.IGNORECASE,
    )

    # ── Narrative-without-execution patterns ──────────────────────────────────

    # Responses that promise a future action without having executed it
    _NARRATIVE_FUTURE_RE = re.compile(
        r"\b(?:"
        r"voy\s+a\s+(?:enviar|mandar|proceder|ejecutar|crear|tomar|hacer)|"
        r"proceder[eé]\s+a|"
        r"enviar[eé]\s+(?:el\s+)?(?:correo|email|informe|reporte)|"
        r"I(?:'ll| will)\s+(?:now\s+)?(?:send|proceed|execute|create|take)"
        r")\b",
        re.IGNORECASE,
    )

    # Specific future-intent: email send — requires gmail:send to have run
    _FUTURE_SEND_RE = re.compile(
        r"\b(?:"
        r"voy\s+a\s+(?:enviar|mandar)\s+(?:el\s+)?(?:correo|email|informe|reporte)|"
        r"(?:enviar|mandar)[eé]\s+(?:el\s+)?(?:correo|email|informe|reporte)|"
        r"proceder[eé]\s+a\s+(?:enviar|mandar)|"
        r"ahora\s+(?:env[ií]o|mando|enviar[eé]|mandar[eé])\s+(?:el\s+)?(?:correo|email)|"
        r"I(?:'ll| will)\s+(?:now\s+)?send\s+the\s+(?:email|report)"
        r")\b",
        re.IGNORECASE,
    )

    # Specific future-intent: screenshot capture — requires browser to have run
    _FUTURE_CAPTURE_RE = re.compile(
        r"\b(?:"
        r"voy\s+a\s+(?:tomar|capturar)\s+(?:una\s+)?(?:captura|screenshot)|"
        r"(?:tomar|capturar)[eé]\s+(?:una\s+)?(?:captura|screenshot)|"
        r"ahora\s+(?:tomo|capturo|tomar[eé]|capturar[eé])\s+(?:una\s+)?(?:captura|screenshot)|"
        r"proceder[eé]\s+a\s+(?:tomar|capturar)"
        r")\b",
        re.IGNORECASE,
    )

    # Generic proceed-to phrases with no grounding
    _FUTURE_PROCEED_RE = re.compile(
        r"\b(?:"
        r"proceder[eé]\s+a|"
        r"ahora\s+(?:voy\s+a\s+)?proceder[eé]|"
        r"a\s+continuaci[oó]n\s+(?:procedo|proceder[eé]|voy\s+a)|"
        r"I(?:'ll| will)\s+now\s+proceed\s+to"
        r")\b",
        re.IGNORECASE,
    )

    # User requests implying an action must execute (not just be described)
    _ACTION_REQUEST_RE = re.compile(
        r"\b(?:env[ií]a(?:me)?|send|manda(?:me)?|captura(?:r)?|screenshot)\b",
        re.IGNORECASE,
    )

    # ── Multi-part request detection ─────────────────────────────────────────

    # Enumeration starters: "1.", "1)", "(1)", "primero", "first"
    _ENUM_START_RE = re.compile(
        r"(?:^|\W)(?:[1-9][.\)]\s|\([1-9]\)|primero[,\s]|segundo[,\s]|"
        r"first[,\s]|second[,\s]|a\)\s|b\)\s)",
        re.IGNORECASE | re.MULTILINE,
    )

    # Connective "also" / "además" signals a second distinct request
    _ALSO_RE = re.compile(
        r"\b(?:adem[aá]s(?:\s+de)?|tambi[eé]n\s+(?:quiero|necesito|dime|dame)|"
        r"y\s+(?:tambi[eé]n|adem[aá]s)|also\s+(?:tell|give|show|can\s+you)|"
        r"additionally|furthermore|por\s+otro\s+lado|igualmente)\b",
        re.IGNORECASE,
    )

    # Response section headers (detect if LLM actually structured its answer)
    _RESPONSE_SECTION_RE = re.compile(
        r"(?:^|\n)\s*(?:\d+[.\)]\s+\S|\*{1,2}\S|\#{1,3}\s+\S|[A-ZÁÉÍÓÚ][A-ZÁÉÍÓÚ\s]{3,}:)",
        re.MULTILINE,
    )

    # ── Placeholder value detection ───────────────────────────────────────────

    # Unfilled template placeholders that slipped through from render_report
    _UNFILLED_TEMPLATE_RE = re.compile(r"\{[a-zA-Z_]\w*\}", re.IGNORECASE)

    # Obvious placeholder literals: $X, $N, $Y (single uppercase letter after $)
    _DOLLAR_PLACEHOLDER_RE = re.compile(r"\$[A-Z]\b")

    # Obvious percentage placeholders: N%, X%, Y% (single uppercase letter before %)
    _PCT_PLACEHOLDER_RE = re.compile(r"\b[A-Z]%\b")

    # Context that indicates this is a data report (not casual chat)
    _REPORT_CONTEXT_RE = re.compile(
        r"\b(?:precio|price|informe|reporte|report|datos|data|btc|eth|bitcoin|ethereum|"
        r"cotizaci[oó]n|porcentaje|variaci[oó]n|tendencia|mercado)\b",
        re.IGNORECASE,
    )

    # ── Public API ────────────────────────────────────────────────────────────

    def validate(
        self,
        response_text: str,
        user_input: str,
        executed_skills: set[str],
        has_any_skill_data: bool,
        planning_mode: bool = False,
        reset_context: dict | None = None,
    ) -> ValidationResult:
        """Run all checks. Returns first failure found, or ok.

        Args:
            response_text:      Final cleaned response about to be sent.
            user_input:         Original user message for this turn.
            executed_skills:    Set of skill names that ran (from trace + auto_results).
            has_any_skill_data: True if capability engine, auto-detect, or LLM skills ran.
            planning_mode:      True if user requested plan-only (no execution). When set,
                                any evidence of execution in the response is a violation.
            reset_context:      If provided, contains {"old_domain": str, "new_domain": str}
                                from an intent switch. Enables cross-context contamination check.
        """
        # Detect user language once — used for all user-facing fallback messages.
        # correction_hints always stay in English (they target the LLM, not the user).
        lang = detect_lang(user_input)

        # Planning mode: first-priority check — must run before all others
        if planning_mode:
            result = self._check_planning_mode_violation(
                response_text, user_input, executed_skills, has_any_skill_data, lang
            )
            if not result.valid:
                return result

        # Context contamination: fires when intent switch occurred this turn
        if reset_context:
            result = self._check_context_contamination(
                response_text, user_input, reset_context, lang
            )
            if not result.valid:
                return result

        checks = [
            self._check_placeholder_output,
            self._check_drift,               # drift before completion — catches substitution first
            self._check_grounding,
            self._check_completion,
            self._check_future_intent_mismatch,
            self._check_narrative_only,
            self._check_action_nonexecution,       # Phase 9: catch passive-instruction responses
            self._check_tracking_false_positive,   # Phase 8: catch hallucinated tracking status
            self._check_completeness_multipart,
        ]
        for check in checks:
            result = check(response_text, user_input, executed_skills, has_any_skill_data, lang)
            if not result.valid:
                return result
        return ValidationResult(valid=True, reason="ok")

    # ── Planning mode violation detection ────────────────────────────────────

    # Execution confirmation phrases that indicate a skill actually ran
    _EXECUTION_CONFIRM_RE = re.compile(
        r"\b(?:"
        r"(?:tarea|task)\s+(?:programada|creada|registrada|scheduled|created)|"
        r"qued[oó]\s+programad[oa]|"
        r"ya\s+(?:est[aá]|qued[oó])\s+(?:programad[oa]|creado|registrado)|"
        r"agente\s+(?:creado|iniciado|en\s+marcha)|"
        r"objetivo\s+(?:registrado|creado|en\s+ejecuci[oó]n)|"
        r"goal\s+(?:created|registered)|agent\s+(?:created|started)|"
        r"correo\s+enviado|email\s+sent|"
        r"se\s+ha\s+(?:creado|programado|registrado|iniciado)"
        r")\b",
        re.IGNORECASE,
    )

    def _check_planning_mode_violation(
        self,
        response: str,
        user_input: str,
        executed_skills: set[str],
        has_data: bool,
        lang: str = "en",
    ) -> ValidationResult:
        """Block responses that show execution evidence when planning mode is active."""
        _EXEC_SKILLS = {
            "task_manager", "agent_manager", "create_goal",
            "scheduler", "cron", "goal_orchestrator",
        }
        _exec_skill_ran = bool(executed_skills & _EXEC_SKILLS)
        _exec_confirmed = bool(self._EXECUTION_CONFIRM_RE.search(response))

        if _exec_skill_ran or _exec_confirmed:
            detail = []
            if _exec_skill_ran:
                detail.append(f"executed skills: {executed_skills & _EXEC_SKILLS}")
            if _exec_confirmed:
                detail.append("response confirms execution")
            return ValidationResult(
                valid=False,
                reason="planning_mode_violation",
                should_retry=True,
                correction_hint=_L["en"]["planning_hint"].format(detail="; ".join(detail)),
                fallback_response=_t(lang, "planning_fail"),
            )
        return ValidationResult(valid=True, reason="ok")

    # ── Context contamination check ───────────────────────────────────────────

    # Domain keywords for contamination detection (compact subset of context_reset.py)
    _CONTAM_CRYPTO = re.compile(
        r"\b(?:btc|bitcoin|eth|ethereum|sol|solana|cripto|crypto|blockchain|"
        r"binance|coinbase|coingecko|cotizaci[oó]n|precio\s+(?:del?\s+)?(?:btc|eth))\b",
        re.IGNORECASE,
    )
    _CONTAM_WEATHER = re.compile(
        r"\b(?:clima|weather|temperatura|temperature|lluvia|forecast|pron[oó]stico|"
        r"tormenta|storm|grados|celsius|fahrenheit|viento|soleado|nublado)\b",
        re.IGNORECASE,
    )
    _CONTAM_CODE = re.compile(
        r"\b(?:c[oó]digo|code|script|funci[oó]n|function|clase|class|variable|bug|"
        r"debug|programaci[oó]n|programming|algoritmo|python|javascript|sql|api\s+endpoint)\b",
        re.IGNORECASE,
    )
    _CONTAM_SHOPPING = re.compile(
        r"\b(?:comprar|buy|tienda|store|zapato|ropa|marca|brand|amazon|aliexpress|"
        r"mercado\s*libre|descuento|oferta|carrito)\b",
        re.IGNORECASE,
    )
    _CONTAM_FOOD = re.compile(
        r"\b(?:receta|recipe|comida|food|cocinar|ingrediente|restaurante|postre|ensalada)\b",
        re.IGNORECASE,
    )
    _CONTAM_NEWS = re.compile(
        r"\b(?:noticia|news|titular|headline|pol[ií]tica|politics|gobierno|elecci[oó]n|guerra)\b",
        re.IGNORECASE,
    )

    _CONTAM_PATTERNS: dict[str, re.Pattern[str]] = {}  # populated below

    def _get_contam_pattern(self, domain: str) -> re.Pattern[str] | None:
        return {
            "crypto": self._CONTAM_CRYPTO,
            "weather": self._CONTAM_WEATHER,
            "code": self._CONTAM_CODE,
            "shopping": self._CONTAM_SHOPPING,
            "food": self._CONTAM_FOOD,
            "news": self._CONTAM_NEWS,
        }.get(domain)

    def _check_context_contamination(
        self,
        response: str,
        user_input: str,
        reset_context: dict,
        lang: str = "en",
    ) -> ValidationResult:
        """Block responses that reference the OLD domain after a hard context reset.

        Fires when an intent switch was detected this turn (reset_context provided).
        Catches: LLM ignoring [CONTEXT RESET] and still answering about the old flow.

        Conservative: only blocks when the response contains strong old-domain
        signals AND contains NO new-domain signals (pure contamination, not synthesis).
        """
        old_domain = reset_context.get("old_domain", "")
        new_domain = reset_context.get("new_domain", "")

        old_pattern = self._get_contam_pattern(old_domain)
        new_pattern = self._get_contam_pattern(new_domain)

        if old_pattern is None:
            return ValidationResult(valid=True, reason="ok")

        old_in_response = bool(old_pattern.search(response))
        if not old_in_response:
            return ValidationResult(valid=True, reason="ok")

        # Old domain present — check if new domain is also present (synthesis, not contamination)
        new_in_response = bool(new_pattern.search(response)) if new_pattern else False
        if new_in_response:
            return ValidationResult(valid=True, reason="ok")  # Mixed → ok, not pure contamination

        # Old domain in response but NOT new domain → context contamination
        return ValidationResult(
            valid=False,
            reason="context_contamination",
            should_retry=True,
            correction_hint=_L["en"]["context_contam_hint"].format(
                old=old_domain, new=new_domain
            ),
            fallback_response=_t(lang, "context_contam_fail"),
        )

    # ── Individual checks ─────────────────────────────────────────────────────

    def _check_placeholder_output(
        self,
        response: str,
        user_input: str,
        executed_skills: set[str],
        has_data: bool,
        lang: str = "en",
    ) -> ValidationResult:
        """Block responses containing unfilled placeholders or dummy values.

        Catches:
        - {varname} — unfilled template placeholders from render_report
        - $X, $N   — literal dollar placeholders
        - N%, X%   — literal percentage placeholders

        Only triggers in data/report contexts to avoid false positives on casual chat.
        """
        # Only check when the response is in a data/reporting context
        if not self._REPORT_CONTEXT_RE.search(user_input) and not self._REPORT_CONTEXT_RE.search(response):
            return ValidationResult(valid=True, reason="ok")

        has_unfilled = bool(self._UNFILLED_TEMPLATE_RE.search(response))
        has_dollar_ph = bool(self._DOLLAR_PLACEHOLDER_RE.search(response))
        has_pct_ph = bool(self._PCT_PLACEHOLDER_RE.search(response))

        if has_unfilled or has_dollar_ph or has_pct_ph:
            detail = []
            if has_unfilled:
                detail.append("unfilled template placeholders {var}")
            if has_dollar_ph:
                detail.append("dollar placeholder ($X)")
            if has_pct_ph:
                detail.append("percentage placeholder (N%)")
            return ValidationResult(
                valid=False,
                reason="placeholder_output",
                should_retry=True,
                correction_hint=_L["en"]["placeholder_hint"].format(detail=", ".join(detail)),
                fallback_response=_t(lang, "placeholder_fail"),
            )
        return ValidationResult(valid=True, reason="ok")

    # Skills that can actually retrieve real-time price/data (browser/screenshot cannot)
    _PRICE_GROUNDING_SKILLS: frozenset[str] = frozenset({
        "web_search", "fetch_url", "http_request", "scrape",
        "browser_deep_scrape", "deep_scraper", "subscribe", "get_weather",
    })

    def _check_grounding(
        self,
        response: str,
        user_input: str,
        executed_skills: set[str],
        has_data: bool,
        lang: str = "en",
    ) -> ValidationResult:
        """Block price/data claims when no price-capable skill retrieved that data.

        IMPORTANT: browser/screenshot skills running does NOT ground price claims.
        Only web_search, fetch_url, http_request, scrape, browser_deep_scrape,
        subscribe, and get_weather can provide real-time data.
        """
        # Price-capable skill ran — response is grounded
        if executed_skills & self._PRICE_GROUNDING_SKILLS:
            return ValidationResult(valid=True, reason="ok")

        # User wasn't asking for real-time data — nothing to ground
        if not self._REALTIME_REQUEST.search(user_input):
            return ValidationResult(valid=True, reason="ok")

        # Response contains no data claims — nothing to check
        if not self._PRICE_CLAIM.search(response):
            return ValidationResult(valid=True, reason="ok")

        # Real-time data requested + price claim in response + no skill ran = hallucination
        return ValidationResult(
            valid=False,
            reason="grounding_fail",
            should_retry=True,
            correction_hint=_L["en"]["grounding_hint"],
            fallback_response=_t(lang, "grounding_fail"),
        )

    def _check_completion(
        self,
        response: str,
        user_input: str,
        executed_skills: set[str],
        has_data: bool,
        lang: str = "en",
    ) -> ValidationResult:
        """Block responses that claim an action was completed when the skill never ran."""

        # Email claimed — only gmail:send counts, NOT gmail:send_check
        if self._EMAIL_CLAIM.search(response):
            _has_send_check = "gmail:send_check" in executed_skills
            _email_ran = (
                "gmail:send" in executed_skills  # precise action-level check (always wins)
                or (
                    # Fallback for legacy paths without action granularity:
                    # only accept plain "gmail" if send_check was never in the picture
                    not _has_send_check
                    and any(
                        s for s in executed_skills
                        if ("gmail" in s or "email" in s or "mail" in s)
                        and "send_check" not in s
                    )
                )
            )
            if not _email_ran:
                # If user never asked for email → don't retry (would try to send one);
                # just use fallback. Only retry when user actually requested email.
                _user_asked_email = bool(self._EMAIL_REQUEST.search(user_input))
                return ValidationResult(
                    valid=False,
                    reason="incomplete",
                    should_retry=_user_asked_email,
                    correction_hint=_L["en"]["email_hint"],
                    fallback_response=_t(lang, "email_fail"),
                )

        # Scheduled task claimed but task_manager never executed
        if self._TASK_CLAIM.search(response):
            _task_ran = any("task_manager" in s for s in executed_skills)
            if not _task_ran and self._SCHEDULE_REQUEST.search(user_input):
                return ValidationResult(
                    valid=False,
                    reason="incomplete",
                    should_retry=True,
                    correction_hint=_L["en"]["task_hint"],
                    fallback_response=_t(lang, "task_fail"),
                )

        return ValidationResult(valid=True, reason="ok")

    def _check_narrative_only(
        self,
        response: str,
        user_input: str,
        executed_skills: set[str],
        has_data: bool,
        lang: str = "en",
    ) -> ValidationResult:
        """Block responses that describe a future action when no skills ran at all.

        Catches: "Voy a enviar el correo ahora..." with zero skill execution.
        Safe: passes when any skill ran (even send_check) or when no action was requested.
        """
        # If any skill executed — response is grounded in real execution
        if has_data:
            return ValidationResult(valid=True, reason="ok")

        # User wasn't requesting an action — narrative is fine (explanation, chat)
        if not self._ACTION_REQUEST_RE.search(user_input):
            return ValidationResult(valid=True, reason="ok")

        # Response doesn't contain future-tense narrative — nothing to block
        if not self._NARRATIVE_FUTURE_RE.search(response):
            return ValidationResult(valid=True, reason="ok")

        # Action requested + future narrative in response + zero skill execution = fake plan
        return ValidationResult(
            valid=False,
            reason="narrative_only",
            should_retry=False,
            fallback_response=_t(lang, "narrative_fail"),
        )

    def _check_future_intent_mismatch(
        self,
        response: str,
        user_input: str,
        executed_skills: set[str],
        has_data: bool,
        lang: str = "en",
    ) -> ValidationResult:
        """Catch future-tense promises for actions that didn't execute — even if other skills ran."""
        # Email send promise without gmail:send execution
        if self._FUTURE_SEND_RE.search(response):
            _email_ran = (
                "gmail:send" in executed_skills
                or any(
                    ("gmail" in s or "email" in s or "mail" in s) and "send_check" not in s
                    for s in executed_skills
                )
            )
            if not _email_ran:
                return ValidationResult(
                    valid=False,
                    reason="future_intent_mismatch",
                    should_retry=True,
                    correction_hint=_L["en"]["future_send_hint"],
                    fallback_response=_t(lang, "future_send_fail"),
                )

        # Screenshot capture promise without browser execution
        if self._FUTURE_CAPTURE_RE.search(response):
            _browser_ran = any("browser" in s for s in executed_skills)
            if not _browser_ran:
                return ValidationResult(
                    valid=False,
                    reason="future_intent_mismatch",
                    should_retry=False,
                    fallback_response=_t(lang, "future_cap_fail"),
                )

        # Generic "procederé a" with no skill execution at all
        if self._FUTURE_PROCEED_RE.search(response) and not has_data:
            return ValidationResult(
                valid=False,
                reason="future_intent_mismatch",
                should_retry=False,
                fallback_response=_t(lang, "future_proc_fail"),
            )

        return ValidationResult(valid=True, reason="ok")

    # ── Action Non-Execution Detector (Phase 9) ───────────────────────────────

    # Passive instruction patterns: agent tells user to do the action themselves
    _PASSIVE_INSTRUCTION_RE = re.compile(
        r"\b(?:"
        r"puedes?\s+(?:ir\s+a|visitar|entrar\s+a|acceder\s+a|navegar\s+a|abrir)|"
        r"you\s+can\s+(?:go\s+to|visit|open|navigate\s+to|access|check)|"
        r"te\s+recomiendo\s+(?:visitar|ir\s+a|abrir|entrar\s+a)|"
        r"I\s+recommend\s+(?:visiting|going\s+to|opening|checking)|"
        r"ingresa(?:\s+al?\s+)?(?:sitio|p[aá]gina|web|link)|"
        r"abre\s+tu\s+(?:navegador|browser)|open\s+your\s+(?:browser|website)|"
        r"visita(?:\s+el|\s+la)?\s+(?:sitio|p[aá]gina|web)|visit\s+the\s+(?:site|website|page)|"
        r"en\s+el\s+sitio\s+(?:web\s+)?de\s+\w+\s+puedes|"
        r"the\s+website\s+(?:allows|lets\s+you|will\s+show)"
        r")\b",
        re.IGNORECASE,
    )

    # Action requests that require agent-side execution (URL or site + action verb)
    _ACTION_WITH_TARGET_RE = re.compile(
        r"\b(?:"
        r"(?:entra|ve|abre|navega|visita|revisa|verifica|chequea|rastrea|captura)\s*"
        r"(?:a\s+|en\s+|al?\s+)?(?:https?://\S+|\w[\w\-]+\.(?:net|com|cl|org|io|co|mx|ar)\S*)|"
        r"(?:go\s+to|open|navigate\s+to|visit|check|browse\s+to)\s+(?:https?://\S+|\w[\w\-]+\.(?:net|com|cl|org)\S*)|"
        r"track\s+(?:package|order|shipment|parcel)|"
        r"rastrear?\s+(?:paquete|pedido|env[ií]o|encomienda)"
        r")\b",
        re.IGNORECASE,
    )

    def _check_action_nonexecution(
        self,
        response: str,
        user_input: str,
        executed_skills: set[str],
        has_data: bool,
        lang: str = "en",
    ) -> ValidationResult:
        """Block responses that give passive instructions when active execution was requested.

        Catches the failure mode: user says 'go to 17track.net and check package X'
        but agent responds 'you can visit 17track.net and enter the tracking code'.

        Conservative rules — all three must be true:
        1. User request contains action verb + external URL/site target
        2. No browser/fetch/exec skill actually ran
        3. Response contains passive instruction patterns ("you can visit...")
        """
        # Pass immediately if any skill ran (agent attempted execution)
        if has_data:
            return ValidationResult(valid=True, reason="ok")

        # Skip if user request doesn't contain an action+target pattern
        if not self._ACTION_WITH_TARGET_RE.search(user_input):
            return ValidationResult(valid=True, reason="ok")

        # Skip if response does NOT contain passive-instruction language
        if not self._PASSIVE_INSTRUCTION_RE.search(response):
            return ValidationResult(valid=True, reason="ok")

        # Check execution set — any attempt counts as pass
        _execution_attempted = any(
            s.startswith(("browser", "fetch_url", "python_exec", "web_search", "http_request"))
            for s in executed_skills
        )
        if _execution_attempted:
            return ValidationResult(valid=True, reason="ok")

        return ValidationResult(
            valid=False,
            reason="action_nonexecution",
            should_retry=True,
            correction_hint=(
                "[VALIDATION BLOCK] The user asked you to PERFORM an action on a website or service. "
                "Your response gives instructions for the user to do it themselves — that is NOT acceptable. "
                "You have browser, python_exec, fetch_url, and web_search skills. USE THEM. "
                "Navigate to the target, interact with it, and report back what you found. "
                "Do NOT tell the user to do things themselves."
            ),
            fallback_response=_t(lang, "narrative_fail"),
        )

    # ── End Action Non-Execution Detector ────────────────────────────────────

    # ── Tracking False-Positive Detector (Phase 8) ───────────────────────────

    # Actual tracking code patterns in user input
    _TRACKING_CODE_IN_INPUT_RE = re.compile(
        r"\b(?:[A-Z]{2}\d{7,11}[A-Z]{2}|1Z[0-9A-Z]{16}|\d{12,22})\b",
    )

    # Tracking request context — user is asking about a package
    _TRACKING_REQUEST_RE = re.compile(
        r"\b(?:"
        r"track(?:ing)?|rastrear?|seguimiento|rastrea|rastreo|17track|"
        r"paquete|package|pedido|order|env[ií]o|shipment|encomienda|"
        r"c[oó]digo\s+de\s+(?:rastreo|seguimiento)|tracking\s+(?:number|code)"
        r")\b",
        re.IGNORECASE,
    )

    # Definitive tracking status claims in response
    _TRACKING_STATUS_CLAIM_RE = re.compile(
        r"\b(?:"
        r"el\s+paquete\s+(?:est[aá]|se\s+encuentra|ha\s+sido|fue)|"
        r"the\s+package\s+(?:is|has\s+been|was)|"
        r"the\s+shipment\s+(?:is|has|was)|"
        r"el\s+env[ií]o\s+(?:est[aá]|ha\s+sido)|"
        r"estado\s+(?:del\s+paquete|de\s+tu\s+pedido|de\s+seguimiento)\s*:\s*\S|"
        r"tracking\s+(?:status|shows?|indicates?)\s*:\s*\S|"
        r"(?:in\s+transit|delivered|out\s+for\s+delivery|shipped)\s+(?:to|on|as\s+of)|"
        r"(?:en\s+tr[aá]nsito|entregado|despachado)\s+(?:el|en|a)"
        r")\b",
        re.IGNORECASE,
    )

    def _check_tracking_false_positive(
        self,
        response: str,
        user_input: str,
        executed_skills: set[str],
        has_data: bool,
        lang: str = "en",
    ) -> ValidationResult:
        """Block responses that claim specific tracking status when browser never ran.

        Catches: user provides tracking code + asks to track, agent claims package
        status without ever executing browser(action='track', ...).

        Conservative: only fires when ALL conditions met:
        1. User input contains actual tracking code pattern
        2. User input contains tracking request context
        3. Response contains definitive tracking status claim (not vague)
        4. No browser skill was executed at all
        """
        # Browser ran — response may be grounded (trust it)
        if any("browser" in s for s in executed_skills):
            return ValidationResult(valid=True, reason="ok")

        # No tracking code in user input — not a tracking request
        if not self._TRACKING_CODE_IN_INPUT_RE.search(user_input):
            return ValidationResult(valid=True, reason="ok")

        # No tracking context keywords — not about packages
        if not self._TRACKING_REQUEST_RE.search(user_input):
            return ValidationResult(valid=True, reason="ok")

        # Response doesn't claim definitive status — no problem
        if not self._TRACKING_STATUS_CLAIM_RE.search(response):
            return ValidationResult(valid=True, reason="ok")

        # All conditions met: tracking request + no browser execution + status claim = hallucination
        return ValidationResult(
            valid=False,
            reason="tracking_false_positive",
            should_retry=True,
            correction_hint=_L["en"]["track_false_positive_hint"],
            fallback_response=_t(lang, "track_false_positive_fail"),
        )

    # ── End Tracking False-Positive Detector ─────────────────────────────────

    def _check_completeness_multipart(
        self,
        response: str,
        user_input: str,
        executed_skills: set[str],
        has_data: bool,
        lang: str = "en",
    ) -> ValidationResult:
        """Block structurally incomplete responses to multi-part requests.

        Triggers when the user clearly asked N≥2 distinct questions/sections
        but the response looks like it only answered one of them (no structural
        markers like numbered sections or headers).

        Conservative: requires strong signal on both the request side (≥2 question
        marks OR enumeration start) AND the response side (very short or zero
        structural markers despite multi-part input).
        """
        # Skip very short inputs — not enough signal
        if len(user_input.strip()) < 30:
            return ValidationResult(valid=True, reason="ok")

        question_marks = user_input.count("?")
        has_enum = bool(self._ENUM_START_RE.search(user_input))
        has_also = bool(self._ALSO_RE.search(user_input))

        # Determine request part count
        part_count = question_marks
        if has_enum:
            enum_matches = self._ENUM_START_RE.findall(user_input)
            part_count = max(part_count, len(enum_matches))
        if has_also and part_count < 2:
            part_count = 2

        # Not a multi-part request
        if part_count < 2:
            return ValidationResult(valid=True, reason="ok")

        # Check if response has structural markers proportional to parts requested
        resp_len = len(response.strip())
        response_sections = len(self._RESPONSE_SECTION_RE.findall(response))
        resp_question_marks = response.count("?")

        # A short response (<120 chars) to a multi-part request is almost certainly incomplete
        if resp_len < 120:
            return ValidationResult(
                valid=False,
                reason="incomplete",
                should_retry=True,
                correction_hint=_L["en"]["multipart_short_hint"].format(n=part_count, chars=resp_len),
                fallback_response=_t(lang, "multipart_short_fail"),
            )

        # If N≥3 parts requested and response has no section markers → likely incomplete
        if part_count >= 3 and response_sections < 2 and resp_len < 500:
            return ValidationResult(
                valid=False,
                reason="incomplete",
                should_retry=True,
                correction_hint=_L["en"]["multipart_struct_hint"].format(n=part_count),
                fallback_response=_t(lang, "multipart_struct_fail"),
            )

        return ValidationResult(valid=True, reason="ok")

    # Browser/screenshot request keywords (all languages)
    _DOMAIN_BROWSER = re.compile(
        r"\b(?:"
        # Spanish
        r"captura(?:s|r)?|capturas?\s+de\s+pantalla|pantalla(?:s)?\s+completa|"
        r"captura\s+(?:completa|entera|toda)|haz\s+scroll|scroll\s+y\s+captura|"
        r"fotograf[ií]a\s+(?:la|el)\s+p[aá]gina|toma\s+(?:una\s+)?(?:captura|foto)|"
        r"navega(?:r)?\s+(?:la|el|hasta)|"
        # English
        r"screenshot(?:s)?|full[\s-]page|take\s+(?:a\s+)?screenshot|"
        r"capture\s+(?:the\s+)?(?:page|site|screen)|scroll\s+(?:and\s+)?(?:capture|screenshot)|"
        # Portuguese
        r"captura(?:r)?\s+(?:a\s+)?p[aá]gina|tirar?\s+screenshot|"
        # French
        r"capture\s+d'[eé]cran|faire?\s+(?:une\s+)?capture|"
        # German
        r"screenshot\s+(?:machen|erstellen|nehmen)|bildschirmfoto"
        r")\b",
        re.IGNORECASE,
    )

    def _check_drift(
        self,
        response: str,
        user_input: str,
        executed_skills: set[str],
        has_data: bool,
        lang: str = "en",
    ) -> ValidationResult:
        """Block responses whose topic is completely unrelated to what the user asked."""
        user_crypto = bool(self._DOMAIN_CRYPTO.search(user_input))
        user_weather = bool(self._DOMAIN_WEATHER.search(user_input))
        user_shopping = bool(self._DOMAIN_SHOPPING.search(user_input))
        user_browser = bool(self._DOMAIN_BROWSER.search(user_input))

        resp_crypto = bool(self._DOMAIN_CRYPTO.search(response))
        resp_weather = bool(self._DOMAIN_WEATHER.search(response))
        resp_shopping = bool(self._DOMAIN_SHOPPING.search(response))

        # Crypto request → weather-only response
        if user_crypto and not resp_crypto and resp_weather:
            return ValidationResult(valid=False, reason="drift",
                                    fallback_response=_t(lang, "drift_fail"))

        # Weather request → crypto-only response
        if user_weather and not resp_weather and resp_crypto:
            return ValidationResult(valid=False, reason="drift",
                                    fallback_response=_t(lang, "drift_fail"))

        # Shopping request → crypto/weather response with no shopping content
        if user_shopping and not resp_shopping and (resp_crypto or resp_weather):
            return ValidationResult(valid=False, reason="drift",
                                    fallback_response=_t(lang, "drift_fail"))

        # Browser/screenshot request → crypto price response when user never asked for prices
        if user_browser and resp_crypto and not user_crypto and not self._REALTIME_REQUEST.search(user_input):
            return ValidationResult(
                valid=False,
                reason="drift",
                should_retry=False,  # No retry — use fallback directly
                correction_hint=_L["en"]["drift_browser_hint"],
                fallback_response=_t(lang, "drift_browser_fail"),
            )

        # Browser request for specific URL → response contains alternative site substitution
        # Catches: user asks for biobiochile.cl, agent responds with Binance data
        _user_url_m = re.search(r"https?://([^\s/]+)", user_input)
        if user_browser and _user_url_m and (resp_crypto or resp_weather):
            _requested_domain = _user_url_m.group(1).lower()
            _resp_has_binance = bool(re.search(r"\bBinance\b", response, re.IGNORECASE))
            _resp_has_coinbase = bool(re.search(r"\bCoinbase\b", response, re.IGNORECASE))
            if (_resp_has_binance or _resp_has_coinbase) and _requested_domain not in ("binance.com", "coinbase.com"):
                return ValidationResult(
                    valid=False,
                    reason="drift",
                    should_retry=False,
                    correction_hint=(
                        f"[VALIDATION BLOCK] The user requested a screenshot of '{_requested_domain}'. "
                        "Your response substituted with Binance/Coinbase data instead. "
                        "NEVER navigate to a different site than what was requested. "
                        f"If '{_requested_domain}' was inaccessible, say ONLY: "
                        f"'I could not access {_requested_domain}. Want me to try again?'"
                    ),
                    fallback_response=_t(lang, "drift_browser_fail"),
                )

        return ValidationResult(valid=True, reason="ok")
