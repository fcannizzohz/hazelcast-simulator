import importlib.machinery
import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[3] / "bin" / "gke_cleanup_project"


def load_script():
    loader = importlib.machinery.SourceFileLoader("gke_cleanup_project", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class GkeCleanupProjectTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.script = load_script()

    def test_created_cluster_state_must_match_plan_identity(self):
        plan = self._plan()
        state = {
            "provider": "gke",
            "project": "project-a",
            "cluster": "cluster-a",
            "location_type": "region",
            "location": "europe-west1",
            "created": True,
        }

        self.assertTrue(self.script.cluster_delete_allowed(plan, state))
        state["cluster"] = "another-cluster"
        self.assertFalse(self.script.cluster_delete_allowed(plan, state))

    def test_existing_cluster_requires_explicit_delete_setting(self):
        plan = self._plan()
        self.assertFalse(self.script.cluster_delete_allowed(plan, {}))

        plan["gke"]["delete_existing_cluster"] = True
        self.assertTrue(self.script.cluster_delete_allowed(plan, {}))

    def test_instance_name_matches_kubernetes_dns_label_normalization(self):
        plan = self._plan()
        plan["kubernetes"]["instance"] = "My GKE_Test"

        self.assertEqual("my-gke-test", self.script.instance_name(plan))

    @staticmethod
    def _plan():
        return {
            "provisioner": "kubernetes",
            "kubernetes": {"provider": "gke", "instance": "test"},
            "gke": {
                "project_id": "project-a",
                "cluster_name": "cluster-a",
                "region": "europe-west1",
            },
        }


if __name__ == "__main__":
    unittest.main()
