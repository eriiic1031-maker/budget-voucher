"""예시 증빙자료 이미지 만들기 (가상 업체·금액). 한 번 만들어 docs/demo_attachments/ 에 넣어 둔다.
    .venv\\Scripts\\python docs\\make_demo_attachments.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "demo_attachments"
OUT.mkdir(exist_ok=True)
F = "C:/Windows/Fonts/malgun.ttf"
FB = "C:/Windows/Fonts/malgunbd.ttf"


def doc(name: str, title: str, issuer: str, rows: list[tuple[str, str]], total: str, foot: list[str]):
    W, H = 1240, 1754   # A4 150dpi
    im = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(im)
    t, b, s = ImageFont.truetype(FB, 64), ImageFont.truetype(FB, 30), ImageFont.truetype(F, 28)
    d.text((W // 2, 150), title, font=t, fill="black", anchor="mm")
    d.text((W // 2, 240), issuer + "  (예시 문서)", font=s, fill="#444", anchor="mm")
    d.line((120, 300, W - 120, 300), fill="black", width=3)
    d.text((120, 340), "수신 : (주)데모회사 총무팀 귀하", font=s, fill="black")
    y = 430
    d.rectangle((120, y, W - 120, y + 60), fill="#EEF2F8", outline="black")
    d.text((140, y + 14), "항목", font=b, fill="black")
    d.text((W - 140, y + 14), "금액", font=b, fill="black", anchor="ra")
    y += 60
    for k, v in rows:
        d.rectangle((120, y, W - 120, y + 60), outline="black")
        d.text((140, y + 14), k, font=s, fill="black")
        d.text((W - 140, y + 14), v, font=s, fill="black", anchor="ra")
        y += 60
    y += 40
    d.text((W - 140, y), total, font=ImageFont.truetype(FB, 40), fill="black", anchor="ra")
    y += 120
    for line in foot:
        d.text((120, y), line, font=s, fill="#333")
        y += 50
    d.text((W // 2, H - 120), "※ 프로그램 시연용 가상 문서입니다.", font=s, fill="#888", anchor="mm")
    im.save(OUT / name)


doc("견적서_대회의실_리모델링.png", "견 적 서", "튼튼설비(주) · 214-81-71000",
    [("대회의실 벽체·천장 철거 및 마감", "14,500,000원"), ("바닥재(카펫타일) 120㎡", "8,400,000원"),
     ("조명·전기 공사", "7,300,000원"), ("AV 설비 배관·배선", "4,800,000원"), ("폐기물 처리·현장 관리", "3,000,000원")],
    "합계 38,000,000원 (부가세 별도)", ["공사 기간 : 2026.10.12 ~ 2026.11.06", "지급 조건 : 1차 기성 100% (10월), 하자보증 1년",
                                     "견적 유효기간 : 2026.10.31"])
doc("정비내역서_엔진수리_34다5678.png", "정 비 내 역 서", "바른카센터(주) · 410-81-00051",
    [("엔진 헤드 가스켓 교환", "3,200,000원"), ("워터펌프·타이밍벨트 교환", "1,900,000원"),
     ("냉각수·엔진오일", "380,000원"), ("공임", "1,320,000원")],
    "합계 6,800,000원 (부가세 별도)", ["차량 : 34다5678 (영업2팀 · 박민수)", "주행거리 : 138,420km", "입고 2026.10.15 · 출고 2026.10.18"])
doc("고지서_재산세_건물분_2026.png", "재산세 고지서 (건물분)", "예시시청 세무과",
    [("재산세 (건물)", "14,820,000원"), ("지방교육세", "2,964,000원"), ("지역자원시설세", "1,826,000원")],
    "납부할 세액 19,610,000원", ["과세 기준일 : 2026.06.01", "납부 기한 : 2026. 7. 31.", "납부처 : 위택스 · 전국 은행"])
print("saved", sorted(p.name for p in OUT.iterdir()))
