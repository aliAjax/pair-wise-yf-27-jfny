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

    def test_full_provenance_and_return_review_flow(self):
        source = self.store.add_source("staff", "馆藏购藏档案", "archive", "ACC-1999-7")
        obj = self.store.create_object("staff", "M-1999-7", "青铜器", "礼器", "市博物馆", "1999年入藏，来源待持续核验。")
        event = self.store.add_event("staff", obj["id"], "acquisition", "1999-07-01", "", "本市", "从私人藏家购入", source["id"], "public")
        evidence = self.store.upload_evidence("staff", obj["id"], "purchase.pdf", base64.b64encode(b"purchase record").decode(), "internal", event["id"])
        self.assertEqual(len(evidence["sha256"]), 64)
        updated = self.store.update_object("staff", obj["id"], {"public_summary": "已完成首轮来源整理。"})
        self.assertEqual(updated["version"], 3)
        claim = self.store.create_claim("claimant1", obj["id"], "王氏家族", "返还藏品")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入调查。")
        self.store.cast_vote("reviewer1", claim["id"], "approve", "来源链清晰，赞成协商。")
        self.store.cast_vote("reviewer2", claim["id"], "approve", "证据充分，同意推进。")
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商返还安排。")
        self.store.transition_claim("reviewer2", claim["id"], "resolved_return", "签署返还协议。")
        public_view = self.store.get_object("public", obj["id"])
        self.assertNotIn("current_holder", public_view)
        self.assertEqual(len(public_view["events"]), 1)
        self.assertEqual(public_view["claims"][0]["status"], "resolved_return")
        claimant_view = self.store.get_object("claimant1", obj["id"])
        self.assertEqual(len(claimant_view["claims"]), 1)
        self.assertGreaterEqual(len(self.store.object_history("reviewer1", obj["id"])), 6)

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

    def test_dual_review_gate_and_disagreement(self):
        obj = self.store.create_object("staff", "M-2002-5", "石雕", "石刻", "库房", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "藏家后人", "返还石雕")
        # 未到待复核阶段不能投票
        with self.assertRaises(BusinessError) as ctx:
            self.store.cast_vote("reviewer1", claim["id"], "approve", "尚未进入复核。")
        self.assertEqual(ctx.exception.code, "invalid_stage")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入复核。")
        # 无人赞成不能推进协商或返还结论
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "negotiating", "尝试直接推进协商。")
        self.assertEqual(ctx.exception.code, "dual_review_required")
        # 只有一票赞成仍不够
        self.store.cast_vote("reviewer1", claim["id"], "approve", "来源材料可信，赞成。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer2", claim["id"], "resolved_return", "尝试直接结案。")
        self.assertEqual(ctx.exception.code, "dual_review_required")
        # 同一审查员不能重复登记
        with self.assertRaises(BusinessError) as ctx:
            self.store.cast_vote("reviewer1", claim["id"], "approve", "重复登记意见。")
        self.assertEqual(ctx.exception.code, "duplicate_vote")
        # 反对票保留分歧记录，主张停在待复核
        self.store.cast_vote("reviewer2", claim["id"], "reject", "关键证据存疑，反对推进。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.transition_claim("reviewer1", claim["id"], "negotiating", "尝试推进协商。")
        self.assertEqual(ctx.exception.code, "review_disagreement")
        self.assertEqual(self.store.get_object("staff", obj["id"])["claims"][0]["status"], "under_review")
        # 工作人员可查看票数、待补票与审查意见
        status = self.store.claim_review_status("staff", claim["id"])
        self.assertEqual((status["approve_count"], status["reject_count"], status["pending_approvals"]), (1, 1, 1))
        self.assertFalse(status["ready"])
        self.assertEqual(status["awaiting_reviewers"], [])
        self.assertEqual({v["decision"] for v in status["votes"]}, {"approve", "reject"})
        # 工作人员在藏品详情可见投票，主张人与公众不可见
        self.assertEqual(len(self.store.get_object("staff", obj["id"])["claims"][0]["votes"]), 2)
        self.assertNotIn("votes", self.store.get_object("claimant1", obj["id"])["claims"][0])
        for outsider in ("claimant1", "public"):
            with self.assertRaises(BusinessError) as ctx:
                self.store.claim_review_status(outsider, claim["id"])
            self.assertEqual(ctx.exception.status, 403)

    def test_dual_review_success_and_self_vote_forbidden(self):
        obj = self.store.create_object("staff", "M-2003-1", "瓷器", "器物", "展厅", "公开简介。")
        claim = self.store.create_claim("claimant1", obj["id"], "原持有人家族", "返还瓷器")
        self.store.transition_claim("reviewer1", claim["id"], "under_review", "材料齐全，进入复核。")
        # 审查员不能给自己的主张投票（直接把主张人改为 reviewer1 模拟）
        with self.store.connect() as conn:
            conn.execute("UPDATE claims SET claimant_id='reviewer1' WHERE id=?", (claim["id"],))
        with self.assertRaises(BusinessError) as ctx:
            self.store.cast_vote("reviewer1", claim["id"], "approve", "给自己的主张投票。")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "self_review_forbidden")
        # 两名不同审查员都赞成后可依次进入协商与返还结论
        self.store.cast_vote("reviewer2", claim["id"], "approve", "证据充分，赞成协商。")
        with self.store.connect() as conn:
            conn.execute("UPDATE claims SET claimant_id='claimant1' WHERE id=?", (claim["id"],))
        self.store.cast_vote("reviewer1", claim["id"], "approve", "复核通过，赞成返还。")
        status = self.store.claim_review_status("staff", claim["id"])
        self.assertTrue(status["ready"])
        self.assertEqual(status["pending_approvals"], 0)
        self.store.transition_claim("reviewer1", claim["id"], "negotiating", "双方开始协商。")
        self.store.transition_claim("reviewer2", claim["id"], "resolved_return", "签署返还协议。")
        self.assertEqual(self.store.get_object("staff", obj["id"])["claims"][0]["status"], "resolved_return")


if __name__ == "__main__":
    unittest.main()
