from answerer import build_messages


def test_prompt_confines_the_model_to_the_passages_and_asks_for_filenames():
    system, user = [m["content"] for m in build_messages("what is x?", [{"filename": "a.pdf", "passage": "x is 1."}])]
    assert "only" in system.lower() and "filename" in system.lower()
    assert "[a.pdf]" in user and "x is 1." in user and "what is x?" in user
