"""CI용 화면 버전 점검: 감지 → 계산 → 전력 테스트 → 시간표 적용까지 실행한다."""

import multiprocessing
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    import app
    a = app.App()
    def fail(*args):
        import traceback; traceback.print_exception(*args); os._exit(1)
    a.report_callback_exception = fail
    def check():
        if a.detect_btn.instate(["disabled"]) or a._calc_job:
            a.after(500, check)
            return
        print(a.info_label.cget("text"))
        print(a.usage_label.cget("text"))
        for i in a.part_tree.get_children():
            print(a.part_tree.item(i)["values"])
        print(a.summary.cget("text"))
        print(a.monitor_summary.cget("text"))
        a.tabs.select(2)
        a.run_tests([("CPU 1코어", 4, "cpu_1"), ("직접 측정", 4, None)])
        a.after(1000, wait_tests)
    def wait_tests():
        if a.test_thread or a.test_queue:
            a.after(500, wait_tests)
            return
        print(a.source_label.cget("text"))
        for i in a.result_tree.get_children():
            print(a.result_tree.item(i)["values"])
        assert a.test_results, "no test results"
        a.test_results[0]["hours"] = 3
        a.apply_activities()
        a.calculate()
        print("activities:", a.summary.cget("text"))
        a.on_close()
    a.after(1000, check)
    a.after(180000, lambda: os._exit(1))
    a.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
