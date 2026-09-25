import base64
import tempfile
import unittest
from pathlib import Path

from app import BusinessError, ProvenanceStore


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ProvenanceStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def make_claim(self):
        self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", None, "public")
        self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        return obj, claim

    def test_full_provenance_and_dual_review_flow(self):
        obj, claim = self.make_claim()
        # 第一名审查员投票后仍停在待复核，第二名补票一致后才进入协商。
        first = self.store.cast_review("reviewer1", claim["id"], "negotiating", "材料充分，建议协商。")
        self.assertEqual(first["result"], "waiting")
        self.assertEqual(first["status"], "under_review")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "negotiating", "单人不能推进协商。")
        self.assertEqual(ctx.exception.code, "dual_review_required")
        second = self.store.cast_review("reviewer2", claim["id"], "negotiating", "同意启动协商。")
        self.assertEqual(second["result"], "unanimous")
        self.assertEqual(second["status"], "negotiating")
        # 协商阶段同样需要两名审查员一致赞成才能给出返还结论。
        self.store.cast_review("reviewer1", claim["id"], "resolved_return", "协议条款齐备。")
        done = self.store.cast_review("reviewer2", claim["id"], "resolved_return", "同意返还。")
        self.assertEqual(done["status"], "resolved_return")

        staff_view = self.store.get_object("staff", obj["id"])
        sc = staff_view["claims"][0]
        self.assertIsNone(sc["open_round"])
        self.assertEqual(sc["final_decision"]["outcome"], "unanimous")
        self.assertEqual(len(sc["final_decision"]["votes"]), 2)
        self.assertGreaterEqual(len(sc["reviews"]), 3)

        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        cc = claimant_view["claims"][0]
        self.assertNotIn("reviews", cc)
        self.assertNotIn("open_round", cc)
        self.assertNotIn("rounds", cc)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 5)

    def test_visibility_and_claim_stage_invariants(self):
        obj = self.store.create_object("staff", "M-2001-2", "手稿", "纸质", "资料室", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "捐赠人后代", "归还手稿")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "resolved_return", "直接结束。")
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertNotIn("claimant_id", self.store.get_object("public", obj["id"])["claims"][0])
        with self.assertRaises(BusinessError) as ctx:
            self.store.add_event("public", obj["id"], "note", "2020-01-01", "", "馆内", "未授权事件", None, "public")
        self.assertEqual(ctx.exception.status, 403)

    def test_split_votes_keep_pending_and_preserve_disagreement(self):
        obj, claim = self.make_claim()
        self.store.cast_review("reviewer1", claim["id"], "negotiating", "建议先行协商。")
        split = self.store.cast_review("reviewer2", claim["id"], "rejected", "材料不足，应驳回。")
        self.assertEqual(split["result"], "split")
        self.assertEqual(split["status"], "under_review")
        self.assertEqual(split["next_round"], 2)

        staff_view = self.store.get_object("staff", obj["id"])
        sc = staff_view["claims"][0]
        self.assertEqual(sc["status"], "under_review")
        self.assertEqual(len(sc["rounds"]), 1)
        self.assertEqual(sc["rounds"][0]["outcome"], "split")
        self.assertEqual(len(sc["rounds"][0]["votes"]), 2)
        self.assertEqual(sc["open_round"]["round"], 2)
        self.assertEqual(sc["open_round"]["pending_count"], 2)
        self.assertEqual([p["reviewer_id"] for p in sc["open_round"]["pending_votes"]], ["reviewer1", "reviewer2"])

        board = self.store.review_board("staff")
        item = next(i for i in board["items"] if i["claim_id"] == claim["id"])
        self.assertEqual(item["status"], "under_review")
        self.assertEqual(item["round"], 2)
        self.assertEqual(item["vote_count"], 0)
        self.assertEqual(item["pending_votes"], ["reviewer1", "reviewer2"])
        self.assertFalse(item["can_viewer_vote"])

        reviewer_board = self.store.review_board("reviewer1")
        ritem = next(i for i in reviewer_board["items"] if i["claim_id"] == claim["id"])
        self.assertTrue(ritem["can_viewer_vote"])

        # 新一轮两人一致后即可进入协商。
        self.store.cast_review("reviewer1", claim["id"], "negotiating", "补充材料后建议协商。")
        done = self.store.cast_review("reviewer2", claim["id"], "negotiating", "同意协商。")
        self.assertEqual(done["result"], "unanimous")
        self.assertEqual(done["status"], "negotiating")

    def test_reviewer_cannot_double_vote_or_review_own_claim(self):
        obj, claim = self.make_claim()
        self.store.cast_review("reviewer1", claim["id"], "negotiating", "建议协商。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.cast_review("reviewer1", claim["id"], "negotiating", "重复登记意见。")
        self.assertEqual(ctx.exception.code, "already_voted")

        # 同一审查员不能既受理又靠接口跳过第二票。
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer2", claim["id"], "negotiating", "试图单人推进。")
        self.assertEqual(ctx.exception.code, "dual_review_required")

        # 工作人员与公众不能投票。
        for uid, expected in [("staff", 403), ("public", 403), ("claimant1", 403)]:
            with self.assertRaises(BusinessError) as ctx:
                self.store.cast_review(uid, claim["id"], "negotiating", "无权投票意见。")
            self.assertEqual(ctx.exception.status, expected)

        with self.assertRaises(BusinessError) as ctx:
            self.store.review_board("claimant1")
        self.assertEqual(ctx.exception.status, 403)

        # 审查员若是主张登记的本人（异常数据场景），受理与投票都必须被拒绝。
        own = self.store.create_claim("claimant1", obj["id"], "某审查员亲属", "归还另一物")
        with self.store.connect() as conn:
            conn.execute("UPDATE claims SET claimant_id='reviewer1' WHERE id=?", (own["id"],))
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", own["id"], "under_review", "受理自己的主张。")
        self.assertEqual(ctx.exception.code, "cannot_review_own_claim")
        with self.store.connect() as conn:
            conn.execute("UPDATE claims SET status='under_review' WHERE id=?", (own["id"],))
        with self.assertRaises(BusinessError) as ctx:
            self.store.cast_review("reviewer1", own["id"], "negotiating", "给自己的主张投票。")
        self.assertEqual(ctx.exception.code, "cannot_review_own_claim")

    def test_unanimous_rejection_closes_claim(self):
        obj, claim = self.make_claim()
        self.store.cast_review("reviewer1", claim["id"], "rejected", "主张缺乏依据。")
        done = self.store.cast_review("reviewer2", claim["id"], "rejected", "同意驳回。")
        self.assertEqual(done["status"], "rejected")
        with self.assertRaises(BusinessError) as ctx:
            self.store.cast_review("reviewer1", claim["id"], "rejected", "终态不能再投。")
        self.assertEqual(ctx.exception.code, "not_votable")


if __name__ == "__main__":
    unittest.main()
