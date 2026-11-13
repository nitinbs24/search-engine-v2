"""Static checks of the minikube manifests against docker-compose.yml and the T21 limits."""
from pathlib import Path

import yaml

HERE = Path(__file__).parent
DOCS = [d for f in ("backing.yaml", "jobs.yaml", "apps.yaml") for d in yaml.safe_load_all((HERE / f).read_text())]
COMPOSE = yaml.safe_load((HERE.parent / "docker-compose.yml").read_text())
GI = 1024**3
UNITS = {"Mi": 1024**2, "Gi": GI}


def containers(kind):
    return [c for d in DOCS if d["kind"] == kind for c in d["spec"]["template"]["spec"]["containers"]]


def mem(c, which):
    v = c["resources"][which]["memory"]
    return int(v[:-2]) * UNITS[v[-2:]]


def test_same_services_as_compose():
    workloads = {d["metadata"]["name"] for d in DOCS if d["kind"] in ("Deployment", "Job")}
    assert workloads == set(COMPOSE["services"])


def test_memory_requests_fit_an_8gb_minikube():
    # Jobs run one at a time; leave ~1 GiB for the minikube system pods.
    total = sum(mem(c, "requests") for c in containers("Deployment")) + max(mem(c, "requests") for c in containers("Job"))
    assert total <= 7 * GI, total / GI
    for c in containers("Deployment") + containers("Job"):
        assert mem(c, "requests") <= mem(c, "limits"), c["name"]


def test_no_gpu_request_by_default():
    assert "nvidia.com/gpu" not in str(DOCS)


def test_credentials_come_from_the_secret():
    for c in containers("Deployment") + containers("Job"):
        for e in c.get("env", []):
            if any(w in e["name"] for w in ("PASSWORD", "SECRET", "ACCESS_KEY")):
                assert "valueFrom" in e, (c["name"], e["name"])


def test_every_service_backend_has_a_readiness_probe():
    services = {d["spec"]["selector"]["app"] for d in DOCS if d["kind"] == "Service"}
    deployed = {d["metadata"]["name"]: d for d in DOCS if d["kind"] == "Deployment"}
    for name in services:
        assert "readinessProbe" in deployed[name]["spec"]["template"]["spec"]["containers"][0], name
