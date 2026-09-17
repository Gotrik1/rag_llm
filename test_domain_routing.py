import unittest

from domain_routing import apply_domain_routing, infer_document_domains, route_query


class DomainRoutingTests(unittest.TestCase):
    def test_auto_routes_price_category_question_to_rre_without_hiding_orem(self):
        route = route_query("Как рассчитывается 6 ЦК?")
        self.assertEqual(route.weights["rre"], 1.0)
        self.assertGreater(route.weights["orem"], 0)
        self.assertEqual(route.active_domains, ("rre",))

    def test_generic_auto_question_does_not_force_an_unrelated_domain(self):
        route = route_query("Как создать новый параметр?")
        self.assertEqual(route.active_domains, ())

    def test_cross_domain_question_reserves_evidence_from_both_groups(self):
        route = route_query("Сравни РРЭ и ОРЭМ", "auto")
        ranked = [
            {"file": "orem.docx", "score": 10.0, "text": "Оптом"},
            {"file": "rre.docx", "score": 2.0, "text": "Розница"},
            {"file": "shared.docx", "score": 9.0, "text": "Общее"},
        ]
        result = apply_domain_routing(
            ranked,
            {"orem.docx": ("orem",), "rre.docx": ("rre",), "shared.docx": ("shared",)},
            route,
            3,
        )
        self.assertEqual({result[0]["file"], result[1]["file"]}, {"orem.docx", "rre.docx"})

    def test_manual_focus_is_soft_not_acl(self):
        route = route_query("Объясни расчёт", "rre")
        ranked = [
            {"file": "orem.docx", "score": 5.0, "text": "Оптом"},
            {"file": "rre.docx", "score": 1.0, "text": "Розница"},
        ]
        result = apply_domain_routing(
            ranked,
            {"orem.docx": ("orem",), "rre.docx": ("rre",)},
            route,
            2,
        )
        self.assertEqual(result[0]["file"], "rre.docx")
        self.assertIn("orem.docx", [item["file"] for item in result])

    def test_direct_lexical_source_survives_incomparable_high_scores(self):
        route = route_query("Почему расчёт договора РРЭ не сформирован?", "rre")
        ranked = [
            {
                "file": "large-manual.docx",
                "score": 20.0 - index / 100,
                "text": f"Связанный фрагмент {index}",
                "matched_queries": ["relationship:section-child"],
            }
            for index in range(40)
        ]
        ranked.append({
            "file": "Методика РРЭ.docx",
            "score": 1.0,
            "text": "Условия формирования расчёта договора РРЭ.",
            "matched_queries": ["domain-lexical"],
        })

        result = apply_domain_routing(
            ranked,
            {
                "large-manual.docx": ("shared",),
                "методика ррэ.docx": ("rre",),
            },
            route,
            10,
        )

        self.assertIn("Методика РРЭ.docx", [item["file"] for item in result])
        self.assertLess(
            [item["file"] for item in result].index("Методика РРЭ.docx"),
            6,
        )

    def test_document_tags_are_multi_label_and_explicit_metadata_wins(self):
        self.assertEqual(infer_document_domains("Методика РРЭ и ОРЭМ.docx"), ("rre", "orem"))
        self.assertEqual(
            infer_document_domains("anything.docx", {"knowledge_domains": ["orem", "shared"]}),
            ("orem", "shared"),
        )


if __name__ == "__main__":
    unittest.main()
