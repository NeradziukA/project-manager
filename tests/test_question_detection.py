"""
Regression tests for QUESTION: marker detection in Claude output.

Bug: Claude sometimes completes partial work and then asks a question
in plain text at the end of its output (without QUESTION: prefix at start).
The old code only checked startswith(QUESTION_MARKER), so the question was
missed and the task was marked as "выполнена". The user's reply then created
a new task without context.

Fix: also detect QUESTION: at the start of the LAST LINE of the output.
"""

QUESTION_MARKER = "QUESTION:"


def detect_question(claude_out: str) -> tuple[bool, str]:
    """Mirrors the detection logic in worker/worker.py process_task()."""
    out_stripped = claude_out.strip()
    last_line = out_stripped.splitlines()[-1].strip() if out_stripped else ""
    has_marker = (
        out_stripped.upper().startswith(QUESTION_MARKER.upper()) or
        last_line.upper().startswith(QUESTION_MARKER.upper())
    )
    if has_marker:
        marker_line = last_line if last_line.upper().startswith(QUESTION_MARKER.upper()) else out_stripped
        question = marker_line[len(QUESTION_MARKER):].strip()
        return True, question
    return False, ""


# ── tests that FAIL without the fix (old code only checked start) ────────────

def test_question_at_end_detected():
    """Claude does partial work, puts QUESTION: on last line — must be caught."""
    output = (
        "Проблема: opacity: 0.01 слишком мал для мобильных.\n\n"
        "QUESTION: Какой opacity хотите? Например, 0.3?"
    )
    found, q = detect_question(output)
    assert found, "question at end of output must be detected"
    assert "0.3" in q


def test_question_at_end_case_insensitive():
    output = "Нашёл файл hexgrid.ts.\nquestion: Использовать значение 0.3?"
    found, q = detect_question(output)
    assert found
    assert "0.3" in q


# ── tests that pass both before and after fix ────────────────────────────────

def test_question_at_start_detected():
    """Original behavior: QUESTION: at the very start."""
    output = "QUESTION: Какое значение opacity использовать?"
    found, q = detect_question(output)
    assert found
    assert "opacity" in q


def test_no_question_in_normal_output():
    """Normal task output with no question must not be flagged."""
    output = "Изменил opacity с 0.01 на 0.3 в hexgrid.ts строка 43."
    found, _ = detect_question(output)
    assert not found


def test_question_mark_in_text_is_not_a_marker():
    """A sentence ending with ? without QUESTION: prefix is not a marker."""
    output = "Готово! Хотите что-то ещё изменить?"
    found, _ = detect_question(output)
    assert not found


def test_empty_output():
    found, _ = detect_question("")
    assert not found


def test_question_extracted_correctly_from_last_line():
    output = "Сделал изменения.\nQUESTION: Подтвердите значение 42?"
    found, q = detect_question(output)
    assert found
    assert q == "Подтвердите значение 42?"
