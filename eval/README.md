# Мини-эвал моделей (шаг 0)

Прогоняет задачи из `tasks/*.json` на моделях из `models.yaml`, исполняет
сгенерированный код в subprocess с таймаутом и печатает таблицу
качество/стоимость. Результат — основание для правок `configs/model_routing.yaml`.

## Запуск

```bash
cd eval
pip install httpx anthropic pyyaml
export ZAI_API_KEY=... DEEPSEEK_API_KEY=... MOONSHOT_API_KEY=... ANTHROPIC_API_KEY=...
python run_eval.py               # все модели, все задачи
python run_eval.py --models glm-5.2 kimi-k2.6   # подмножество
```

Ожидаемая стоимость полного прогона (5 задач × 4 модели, ~2-4K токенов на задачу):
**единицы центов — максимум ~$0.5**. Расширение до 20 задач — всё ещё <$2.

## Формат задачи

```json
{
  "id": "task_id",
  "kind": "exact" | "maximize",
  "statement": "постановка (модель видит её)",
  "function_name": "solve",
  "timeout_sec": 15,
  "tests": [{"args": [...], "expected": ...}],   // kind=exact
  "scorer": "def score(result, args): ...\n"     // kind=maximize, возвращает 0..1
}
```

`exact` — метрика pass-rate по тестам. `maximize` — средний нормированный score
(1.0 = известный оптимум). Добавляй задачи файлами — харнесс подхватит сам.
