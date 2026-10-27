"""AIへのお願い文(prompt.txt)を作ります。自動更新の中で、最初に呼ばれます。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import update_data as u

data = u.load_data()
text = u.build_prompt(data, u.today_jst())
text += (
    "\n\n【出力のしかた】答えの JSON は、チャットに書かず、ファイル answer.json に保存してください。"
    "ファイルには JSON だけを書き、前後に説明文や ``` を付けないでください。"
)
pathlib.Path("prompt.txt").write_text(text, encoding="utf-8")
print("prompt.txt を作りました(", len(text), "文字 )")
