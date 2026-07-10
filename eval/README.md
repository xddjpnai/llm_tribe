# Мини-эвал моделей (шаг 0, до деплоя)

Прогоняет `tasks/*.json` на моделях из `models.yaml`, исполняет сгенерированный
код в subprocess с таймаутом, печатает таблицу качество/стоимость. Результат —
основание для правок `configs/model_routing.yaml`. Отдельно от рантайма.

    python3 -m venv .venv && .venv/bin/pip install httpx anthropic pyyaml
    export ZAI_API_KEY=... DEEPSEEK_API_KEY=... MOONSHOT_API_KEY=... ANTHROPIC_API_KEY=...
    .venv/bin/python run_eval.py            # все модели/задачи; стоит центы
    .venv/bin/python run_eval.py --models glm-5.2 kimi-k2.6

Формат задачи: `{id, kind: exact|maximize, statement, function_name, timeout_sec,
tests:[...], scorer?}`. `exact` — pass-rate по тестам; `maximize` — средний
score из `scorer` (0..1).
