# FOMO Copy Bot: bot copy trade memecoin giống trong video

Bot này làm đúng những gì video quảng cáo:

- Lấy **top trader tuần** trên FOMO (6 người), copy người **#1**.
- Trader mua coin nào thì bot mua coin đó, **size 0.25x** lệnh của họ (có giới hạn tối đa).
- **Trader bán thì bot bán**, đúng tỷ lệ họ bán (bán 50% thì mình bán 50%).
- **"2 L in a row"**: copy thua 2 lệnh liên tiếp thì **đuổi (FIRED)** trader đó và chuyển sang người kế tiếp trong top tuần.
- Dashboard giống video: số dư, P&L, trader đang copy, luật đuổi, biểu đồ số dư có đánh dấu lúc đuổi, copy feed, danh sách trader.

Ngoài luật trong video, bot có thêm những thứ video không nói tới nhưng **bắt buộc phải có** khi đánh memecoin: kiểm tra rug/honeypot trước khi mua, stop loss, giới hạn mỗi lệnh, kill switch.

> **Đọc phần "Sự thật về video" ở dưới trước khi bỏ tiền thật.**
>
> **Mới dùng lần đầu? Làm theo [`HUONG_DAN.md`](HUONG_DAN.md): từng bước từ A đến Z để chạy live 24/24 trên MacBook.**

---

## Sự thật về video

Video là quảng cáo của **copyfomo**, một bot Telegram/Chrome do bên thứ ba làm, **không phải của FOMO**. Màn hình trong video ghi rõ "executed by copyfomo". Từ $42 lên $5.739 trong 24 giờ (x136) là **hoạt hình quảng cáo, không phải tài khoản thật**:

- DWF Ventures (tháng 8/2026) phân tích khoảng 292.000 ví FOMO: chỉ **6,16% có lãi** sau 3 tháng, tức khoảng **94% lỗ**. Một nghiên cứu khác dựa trên dữ liệu Bitquery cho kết quả tương tự: khoảng **90% ví lỗ sau phí** (con số này tôi chưa kiểm chứng được phương pháp).
- Lý do theo DWF: lỗ đến từ **cấu trúc**, không phải do xui. Người được copy có lãi khi follower mua vào đẩy giá lên, còn follower gánh lỗ khi người đó bán ra.
- Lệnh copy **luôn đến sau** trader vài giây. Trong vài giây đó, đám đông copy khác đã đẩy giá lên trước khi mình mua, và bán tháo trước khi mình bán.
- Luật "2 L in a row" có điểm yếu: trong demo, một trader đang làm mình lãi +$129 vẫn bị đuổi chỉ vì 2 lệnh thua liền.

Bot này làm đúng như video, nhưng **không thể** biến chiến lược lỗ thành lãi. Vì vậy mới có lệnh `research` (chạy lại luật trên giao dịch thật 7 ngày qua) và bước paper trading trước khi chạy live.

---

## Cài đặt trên MacBook

Mở **Terminal** (Cmd+Space, gõ "Terminal"). Cài uv (chỉ 1 lần), xong thì **đóng hẳn Terminal (Cmd+Q) và mở lại**:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Rồi tải bot:

```bash
git clone -b claude/determined-goldberg-r7aoiv https://github.com/leonguyen2308-sketch/phantom-trading-bot
cd phantom-trading-bot
```

(Các lệnh trong tài liệu này không kèm ghi chú `# ...`: Terminal của Mac (zsh) không bỏ qua ghi chú mà đưa luôn vào lệnh, làm lệnh lỗi.)

### Bước 1: Xem demo (không cần key, không cần tiền)

```bash
./fomo.sh demo
```

Mở trình duyệt vào **http://localhost:8001**. Bạn sẽ thấy dashboard giống video, chạy với 6 trader và coin **giả lập**, nhanh gấp 60 lần (1 phút thật = 1 giờ giả lập). Bot copy lệnh, bán theo trader, đuổi trader khi thua 2 lệnh liền, và bỏ qua coin nguy hiểm. Bấm Ctrl+C để dừng. `./fomo.sh demo --speed 300` chạy nhanh hơn nữa.

Demo có mô phỏng bất lợi của người copy (giá bị đẩy lên trước khi mình mua và bị bán tháo trước khi mình bán), nên kết quả mỗi lần một khác, có lúc lãi có lúc lỗ. **Con số trong demo không nói lên gì về kết quả thật.**

### Bước 2: Lấy các key miễn phí

```bash
open -e .env
```

Điền vào file `.env`:

| Key | Lấy ở đâu | Để làm gì |
|---|---|---|
| `SOLANA_RPC_URL` | [helius.dev](https://helius.dev) → đăng ký gói Free → copy "RPC URL" (dạng `https://mainnet.helius-rpc.com/?api-key=...`) | Theo dõi ví trader theo thời gian thực |
| `JUPITER_API_KEY` | [portal.jup.ag](https://portal.jup.ag) → tạo API key | Báo giá và khớp lệnh mua/bán |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Không bắt buộc. Tạo bot với @BotFather, lấy chat id từ @userinfobot | Nhận thông báo mỗi lệnh; gửi `/status`, `/kill` từ điện thoại |

Gói Free của Helius (1 triệu credit/tháng, 10 request/giây) đủ để copy 1 trader. Bot dùng WebSocket nên gần như không tốn credit khi trader không giao dịch.

### Bước 3: Danh sách top trader tuần + ví của họ

App FOMO **ẩn địa chỉ ví** và **không có API chính thức**. Có hai cách:

**Cách A (tự nhập, miễn phí):** mở app FOMO → Leaderboard → tab tuần (7D), ghi lại 6 trader đầu. Tìm ví Solana của từng người bằng một công cụ "FOMO wallet finder" (ví dụ [fomowalletfinder.com](https://fomowalletfinder.com), [fomolens.app](https://fomolens.app), hoặc extension Chrome của copyfomo). Đây đều là công cụ **không chính thức**: hãy kiểm tra lại bằng lệnh `traders` bên dưới. Sau đó mở `config/traders.yaml` và điền theo thứ tự hạng:

```yaml
traders:
  - handle: ten_trader_1
    wallet: DIA_CHI_VI_SOLANA_1
  - handle: ten_trader_2
    wallet: DIA_CHI_VI_SOLANA_2
```

**Cách B (tự động, có thể tốn phí):** đăng ký key ở [fomoapi.io](https://fomoapi.io) (bên thứ ba, có gói free) và điền `FOMOAPI_KEY=` trong `.env`. Bot sẽ tự tải top tuần kèm ví mỗi 24 giờ.

Kiểm tra xem bot có **nhìn thấy** lệnh của từng trader không:

```bash
./fomo.sh traders
```

Mỗi trader phải hiện dòng `N trades found ... OK`. Nếu thấy `NO trades found` thì ví sai, trader không hoạt động, hoặc họ đang trade trên chain khác (FOMO còn có Base, BNB; bot này chỉ copy được trên Solana).

### Bước 4: Nghiên cứu trước khi bỏ tiền

```bash
./fomo.sh research
```

Lệnh này đọc giao dịch **thật** trong 7 ngày qua của các trader, rồi cho bạn biết:

1. Từng trader thật sự thắng bao nhiêu % (thường khác xa con số trên leaderboard).
2. Nếu 7 ngày qua bạn copy họ đúng theo luật video, bắt đầu với $100, thì cuối cùng còn bao nhiêu, ở 3 mức trượt giá: 0% (không thể đạt được), 3% (bot nhanh) và 8% (gần với máy Mac ở nhà khi copy coin đang hot).

Nếu ở mức 3% và 8% mà vẫn lỗ thì đừng chạy live với những trader đó.

### Bước 5: Paper trading (tiền giả, giá thật), ít nhất 24 giờ

```bash
./fomo.sh paper
```

Dashboard ở **http://localhost:8001**. Bot copy trader thật, lấy giá thật từ Jupiter, nhưng chỉ dùng tiền giả ($100, đổi bằng `FOMO_PAPER_START_USD`). Mỗi lệnh tự trừ thêm 1,5% trượt giá cho vài giây trễ. Muốn bot chạy nền, kể cả sau khi khởi động lại Mac:

```bash
scripts/install-fomo-service.sh paper
```

### Bước 6: Chạy LIVE bằng tiền thật

Chỉ chạy khi paper trading đã ổn. Các bước:

1. Trong Phantom, **tạo ví MỚI** chỉ dùng cho bot này. **Không bao giờ** dùng ví chính.
2. Gửi vào ví đó **USDC (mạng Solana)**, chỉ số tiền bạn chấp nhận mất hết, và khoảng **0,1 SOL**. Mỗi coin mới mua giữ lại khoảng 0,002 SOL tiền "rent" cho tài khoản token; còn dưới 0,03 SOL thì bot ngừng copy.
3. Phantom → Settings → chọn ví bot → **Show Private Key**. Dán vào `.env` ở `FOMO_BOT_PRIVATE_KEY`, và địa chỉ ví vào `FOMO_BOT_ADDRESS`. Không dán key này ở bất kỳ chỗ nào khác.
4. Chạy lần lượt: `./fomo.sh wallet-check` (xem số dư và thử báo giá, không gửi gì cả), `./fomo.sh approve` (gõ APPROVE), `./fomo.sh ack-risks` (gõ I ACCEPT), `./fomo.sh golive-check` (mọi dòng phải PASS):

```bash
./fomo.sh wallet-check
./fomo.sh approve
./fomo.sh ack-risks
./fomo.sh golive-check
```

5. Trong `.env` sửa thành `FOMO_LIVE=yes`, rồi chạy nền 24/24:

```bash
scripts/install-fomo-service.sh live
```

(Hoặc chạy trong cửa sổ Terminal đang mở bằng `./fomo.sh live`.)

`golive-check` yêu cầu đã paper ít nhất 24 giờ và có ít nhất 5 lệnh copy. Mục đích là chứng minh máy bạn thực sự bắt được lệnh của trader và Jupiter khớp được lệnh, trước khi dùng tiền thật.

---

## Kill switch: dừng khẩn cấp

Kill switch **bán hết mọi coin và ngừng copy**. Nó tự bật khi:

- Số dư giảm 40% so với mức cao nhất.
- API lỗi liên tục khoảng 2,5 phút (mất mạng, RPC sập).
- File `config/fomo.yaml` bị sửa trong lúc đang chạy live.

Bạn tự bật được bằng nút **KILL SWITCH** trên dashboard, lệnh `/kill` trên Telegram, hoặc `./fomo.sh kill`. Muốn copy lại thì chạy `./fomo.sh reset` (gõ RESET).

**Muốn sửa cấu hình khi đang live:** dừng bot (Ctrl+C) → sửa file → `./fomo.sh approve` → chạy lại. Đừng rút tiền khỏi ví bot khi bot đang chạy, vì bot sẽ hiểu là lỗ và có thể bật kill switch.

---

## Cấu hình (`config/fomo.yaml`)

| Mục | Mặc định | Ý nghĩa |
|---|---|---|
| `follow_all` | true | Copy **cả danh sách cùng lúc** (top 1 tới 500). `false` = cách cũ trong video: chỉ 1 ví, đuổi rồi sang ví kế tiếp. Xem [`HUONG_DAN_COPY500.md`](HUONG_DAN_COPY500.md) |
| `lineup_size` | 500 | Copy top bao nhiêu ví (1 tới 500; cách cũ dùng 6) |
| `copy_ratio` | 0.25 | Size = lệnh của trader × 0.25 (giống video) |
| `max_trade_usd` | 5 | Mỗi lệnh copy tối đa $5 (cách cũ: $20) |
| `min_trade_usd` | 2 | Lệnh nhỏ hơn $2 thì bỏ qua |
| `fire_after_losses` | 3 | Thua 3 lệnh liền thì đuổi trader (7 ngày); cách cũ: 2 |
| `watch.*` | 5 x 100 | Giới hạn gói miễn phí khi đọc 500 ví: 5 kết nối WebSocket x 100 ví, 120 lệnh/giờ/ví tối đa, 1.000 lần đọc giao dịch/giờ |
| `max_chase_pct` | 20 | Giá đã cao hơn giá trader mua 20% thì bỏ qua |
| `stop_loss_pct` | 50 | Coin giảm 50% thì bán (video không có, nhưng nên có) |
| `max_hold_hours` | 24 | Giữ quá 24 giờ thì bán (cách cũ: 48) |
| `min_liquidity_usd` | 15000 | Pool dưới $15k thanh khoản thì bỏ qua |
| `check_sell_route` | true | Thử báo giá bán ngược lại ngay: không bán được = honeypot, bỏ qua |
| `max_open_positions` | 15 | Tối đa 15 coin cùng lúc (cách cũ: 5) |
| `max_drawdown_pct` | 0.40 | Giảm 40% từ đỉnh thì kill switch |
| `daily_loss_limit_pct` | 0.20 | Lỗ 20% trong ngày thì ngừng mua tới 00:00 UTC (7 giờ sáng giờ Việt Nam) |

Nhiều coin trader FOMO mua là coin **pump.fun chưa lên sàn** (vẫn đang ở bonding curve, chưa có pool). Mặc định bot bỏ qua loại này vì rủi ro rất cao. Muốn copy cả loại này thì đặt `min_liquidity_usd: 0`.

---

## Những gì bot KHÔNG làm được

- **Không nhanh bằng bot chuyên nghiệp.** Máy Mac ở nhà mất khoảng 2 đến 6 giây từ lúc trader mua tới lúc mình mua. Bot thương mại đặt server cạnh validator, nhanh hơn nhiều.
- **Không copy được trên Base/BNB.** Chỉ copy được trên Solana.
- **Không thấy lệnh bán nếu trader chuyển coin sang ví khác rồi mới bán.** Khi đó stop loss và giới hạn 48 giờ sẽ xử lý.
- **Stop loss bán sau khi giá đã rơi**, không bán trước. Coin rug thì vẫn lỗ gần hết lệnh đó.
- **Mac ngủ hoặc mất mạng thì bot mù.** Hãy cắm sạc và dùng `install-fomo-service.sh`, script này giữ Mac không tự ngủ.

---

## Nghiên cứu: cách bot hoạt động bên trong

- **FOMO** là app trade memecoin dạng mạng xã hội. Bitquery cho biết hầu hết lệnh FOMO đi qua aggregator **DFlow**, và FOMO dùng một ví riêng (`AgmLJBMD...`) để ký và trả phí gas cho người dùng. Vì vậy bot theo dõi **cả ví trader lẫn tài khoản USDC** của họ, và đọc lệnh từ thay đổi số dư token, không phụ thuộc vào DEX nào.
- **Phát hiện lệnh:** WebSocket `logsSubscribe` của Solana (đẩy về trong khoảng 1 giây), có polling dự phòng mỗi 30 giây (3 giây nếu WebSocket rớt). Lệnh cũ hơn 60 giây thì không copy.
- **Khớp lệnh:** Jupiter Swap API v2 (`/order` → ký → `/execute`), mua và bán bằng USDC. Trước khi ký, bot kiểm tra giao dịch: từ chối nếu có lệnh approve token, đổi quyền sở hữu, hoặc chuyển SOL ra ngoài (dùng chung bộ kiểm tra với bot BTC).
- **Kiểm tra coin trước khi mua:** mint authority và freeze authority phải đã bị thu hồi; chặn các extension Token-2022 nguy hiểm (transfer hook, permanent delegate, transfer fee...); đủ thanh khoản và vốn hoá (DexScreener); price impact không quá cao; báo giá bán ngược phải có đường và không lỗ quá 15%.

Nguồn:
- [DWF Ventures: ~94% ví copy trade trên Fomo lỗ (Yellow)](https://yellow.com/news/fomo-copy-trading-94-percent-wallet-losses)
- [Bitquery: FOMO App Trades API (ví ký lệnh, DFlow, phí USDC)](https://docs.bitquery.io/docs/blockchain/Solana/fomo-api/)
- [FOMO doanh thu tuần giảm 59% (Crypto Briefing)](https://cryptobriefing.com/fomo-weekly-revenue-drops-59-percent/)
- [FOMO gọi vốn Series B, định giá $550M (Cointelegraph)](https://cointelegraph.com/news/fomo-raise-series-b-trading-app-550m-valuation)
- [copyfomo: tài liệu cài đặt (bên thứ ba)](https://www.copyfomo.com/docs)
- [fomoapi.io: API leaderboard không chính thức](https://fomoapi.io/docs)
- [Jupiter Swap API](https://developers.jup.ag/docs/swap) · [Jupiter Price API v3](https://developers.jup.ag/docs/price)
- [Helius: bảng giá](https://www.helius.dev/pricing)

---

## Các lệnh

| Lệnh | Làm gì |
|---|---|
| `./fomo.sh demo` | mô phỏng + dashboard, không cần key |
| `./fomo.sh demo --traders 200` | mô phỏng với 200 ví giả (mặc định 40) |
| `./fomo.sh traders` | xem top ví (lưu đủ danh sách vào `reports/fomo_top.txt`), kiểm tra 8 ví đầu (`--check N` để đổi số) |
| `./fomo.sh research --top 10` | chạy lại luật copy trên giao dịch thật 7 ngày qua của N ví đầu (mỗi ví tốn hàng trăm lần gọi RPC) |
| `./fomo.sh paper` | copy thật, tiền giả |
| `./fomo.sh wallet-check` | kiểm tra ví bot (không gửi gì) |
| `./fomo.sh approve` | khoá `config/fomo.yaml` |
| `./fomo.sh ack-risks` | đọc và chấp nhận rủi ro |
| `./fomo.sh golive-check` | kiểm tra trước khi chạy tiền thật |
| `./fomo.sh live` | tiền thật |
| `./fomo.sh kill` | bán hết, ngừng copy |
| `./fomo.sh reset` | bật lại sau kill switch |

Code nằm trong `fomo/`, test nằm trong `tests/test_fomo_*.py` (chạy bằng `uv run --group dev pytest`).
