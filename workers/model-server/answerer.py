"""Optional answer generation: a small instruct model that answers only from the given passages."""

SYSTEM = ("Answer the question using only the passages provided. Cite the filename of each passage you "
          "use in square brackets, like [report.pdf]. If the passages do not contain the answer, say so. Be brief.")


def build_messages(question: str, sources: list[dict]) -> list[dict]:
    passages = "\n\n".join(f"[{s['filename']}]\n{s['passage']}" for s in sources)
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"{passages}\n\nQuestion: {question}"}]


class Answerer:
    def __init__(self, name: str):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.name = name
        self.tokenizer = AutoTokenizer.from_pretrained(name)
        self.model = AutoModelForCausalLM.from_pretrained(name, torch_dtype=torch.float16).to("cuda")

    def answer(self, question: str, sources: list[dict]) -> str:
        ids = self.tokenizer.apply_chat_template(build_messages(question, sources), add_generation_prompt=True,
                                                 return_tensors="pt").to("cuda")
        out = self.model.generate(ids, max_new_tokens=200, do_sample=False)
        return self.tokenizer.decode(out[0, ids.shape[1]:], skip_special_tokens=True).strip()
