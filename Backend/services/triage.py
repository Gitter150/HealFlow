class TriageEngine:
    """OO boundary around the existing, unchanged symptom/history algorithm."""

    def assess_severity(self, symptom: str, medical_text: str = "") -> int:
        from triage_engine import calculate_dynamic_priority
        return calculate_dynamic_priority(symptom, medical_text)

    def extract_medical_text(self, content: bytes, filename: str) -> str:
        from triage_engine import extract_text
        return extract_text(content, filename)
