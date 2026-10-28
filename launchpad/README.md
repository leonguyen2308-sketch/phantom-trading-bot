# kimlaunch: launchpad meme coin chủ đề ông Kim

Web tiếng Anh, giống uselaunch.fun, chạy trên **Raydium LaunchLab**. Người dùng kết nối Phantom, tạo coin
("build your own rocket here"), và coin có bonding curve giao dịch được ngay. Khi curve đủ 85 SOL, coin tự
lên Raydium.

- **Mỗi lần tạo coin trả 0.01 SOL** vào ví `DFAcvisLammoF7ZDLJWyC47EqKzXqWRWgwdNS34qa9Lj`. Phí nằm **chung
  một giao dịch** với lệnh tạo coin: tạo coin thất bại thì không bị trừ phí. Người dùng thấy rõ khoản phí
  trên web và trong Phantom trước khi ký.
- **Dev buy (tùy chọn):** người tạo mua trước ngay trong giao dịch tạo coin, nên bot không mua chen trước được.
- **Ảnh và thông tin coin** được lưu vĩnh viễn trên Arweave, miễn phí, không cần API key.
- **Bảng coin live** đọc thẳng từ blockchain. Chỉ coin nào **đã trả phí** mới được hiện. Ticker hoặc tên trùng
  với coin đã có trên bảng sẽ bị từ chối.
- **Trang coin** có mua/bán ngay trên bonding curve. Coin đã lên Raydium thì có nút sang Raydium, Jupiter,
  DexScreener.
- Không có server, không có database: chỉ cần một thư mục web tĩnh (`site/`).

## 1. Chỉnh cấu hình (`site/config.js`)

Mở `site/config.js`, sửa rồi lưu. Không cần build lại.

| Mục | Ý nghĩa |
|---|---|
| `siteName` | Tên hiện trên web (mặc định `kimlaunch`) |
| `siteUrl` | Địa chỉ web của bạn, ví dụ `https://kimlaunch.fun` (ghi vào metadata của từng coin) |
| `network` | `"mainnet"` để chạy thật, `"devnet"` để thử bằng SOL ảo |
| `rpcUrl` | **Rất nên điền.** Để trống thì web dùng RPC công cộng miễn phí: chậm, lúc được lúc không |
| `feeWallet` | Ví nhận phí (đã điền sẵn ví của bạn) |
| `feeSol` | Phí mỗi lần tạo coin (đang là `0.01`) |
| `links` | Link X / Telegram của bạn (để trống thì ẩn) |
| `hide` | Ẩn coin khỏi bảng (lừa đảo, tên xấu, coin nhái): ghi địa chỉ coin hoặc ticker, ví dụ `hide: ["SCAM"]` |

**Lấy `rpcUrl` miễn phí:** đăng ký tại [helius.dev](https://www.helius.dev), copy API key, rồi điền:

```js
rpcUrl: "https://mainnet.helius-rpc.com/?api-key=KEY_CUA_BAN",
```

Key này nằm công khai trong web (web nào cũng vậy). Nếu Helius có mục giới hạn domain, thêm domain web của bạn
vào đó để người khác không xài ké.

## 2. Thử trước trên devnet (SOL ảo, không mất tiền)

1. Trong `site/config.js` đặt `network: "devnet"`. `rpcUrl` để trống, hoặc dùng
   `https://devnet.helius-rpc.com/?api-key=KEY_CUA_BAN`.
2. Phantom → Settings → Developer Settings → bật **Testnet Mode**, chọn **Solana Devnet**.
3. Lấy SOL ảo miễn phí tại [faucet.solana.com](https://faucet.solana.com).
4. Mở web, tạo thử một coin có dev buy 0.1 SOL. Sau đó kiểm tra:
   - Ví phí đã nhận 0.01 SOL: mở `https://solscan.io/account/DFAcvisLammoF7ZDLJWyC47EqKzXqWRWgwdNS34qa9Lj?cluster=devnet`.
   - Coin hiện trên bảng, mở trang coin rồi thử mua và bán.
5. Thử xong thì đổi lại `network: "mainnet"` và điền `rpcUrl` mainnet.

Nếu web báo `LaunchLab config ... was not found on devnet`, nghĩa là Raydium đã đổi bản devnet của họ. Khi
đó bỏ qua bước này và làm bước kiểm tra nhỏ trên mainnet ở mục 4.

## 3. Đưa web lên mạng (miễn phí)

Chỉ cần upload **thư mục `launchpad/site`**. File `assets/app.js` đã build sẵn.

**Cách nhanh nhất: Netlify Drop**
1. Vào [app.netlify.com/drop](https://app.netlify.com/drop) và đăng nhập.
2. Kéo thả thư mục `launchpad/site` vào trang. Vài giây sau web đã live ở địa chỉ `*.netlify.app`.
3. Muốn đổi `config.js`: sửa file, rồi kéo thả lại thư mục vào phần Deploys của site đó.

**Tự cập nhật theo GitHub: Cloudflare Pages**
1. [dash.cloudflare.com](https://dash.cloudflare.com) → Workers & Pages → Create → Pages → Connect to Git →
   chọn repo `phantom-trading-bot` và nhánh có code này.
2. Framework preset: **None**. Build command: **để trống**. Build output directory: `launchpad/site`.
   (Nếu Cloudflare báo lỗi, đặt Root directory là `launchpad/site` và Build output directory là `/`.)
3. Deploy. Từ đó mỗi lần push lên GitHub, web tự cập nhật.

Cả hai đều cho gắn tên miền riêng, ví dụ `kimlaunch.fun`. Có tên miền rồi thì:
- Điền `siteUrl` trong `config.js`.
- Trong `site/index.html`, đổi hai chỗ `content="assets/og.jpg"` thành địa chỉ đầy đủ, ví dụ
  `https://kimlaunch.fun/assets/og.jpg`. Ảnh xem trước khi share link lên X/Telegram mới hiện đúng.

## 4. Trước khi mở cho mọi người

1. Trên mainnet, tự tạo một coin **không dev buy** (tốn khoảng 0.026 SOL: 0.01 phí + khoảng 0.015 tiền
   thuê tài khoản Raydium + phí mạng).
2. Mở Solscan của ví phí, kiểm tra có giao dịch **+0.01 SOL**.
3. Tạo coin ông Kim của bạn. Coin nào vốn hóa lớn nhất mà còn trên curve sẽ hiện ở ô **"supreme rocket"**.

## Những điều cần biết

- **Chưa chạy thật trên Solana.** Máy làm ra code này không vào được mạng Solana. Phần tạo giao dịch đã được
  so khớp từng byte với SDK chính thức của Raydium. Toàn bộ web đã được chạy thử trên trình duyệt với
  blockchain và ví Phantom giả lập (kết nối ví, tạo coin, phí 0.01 SOL, dev buy, mua, bán). Dù vậy, vẫn cần
  làm mục 2 hoặc mục 4 trước khi quảng bá.
- **Tiền phí giao dịch trên curve** (khoảng 1%) thuộc về Raydium. Bạn chỉ nhận 0.01 SOL mỗi lần tạo coin. Muốn
  ăn thêm phí giao dịch như uselaunch.fun thì phải tạo "platform" LaunchLab riêng. Đây là bước làm thêm sau
  này: điền id vào `platformId`.
- **RPC miễn phí có giới hạn số lượt gọi mỗi tháng.** Mỗi người đang mở web gọi RPC khoảng 2 lần mỗi 30 giây.
  Nếu web đông người, nâng gói RPC (Helius hoặc QuickNode).
- **Lưu ảnh miễn phí** dùng dịch vụ Turbo của ar.io (miễn phí cho file dưới khoảng 100 KB, web tự nén ảnh). Nếu
  sau này Turbo đổi chính sách, phần upload ảnh sẽ báo lỗi và cần chuyển sang dịch vụ khác (ví dụ Pinata).
- Web là **parody**: footer và trang "how it works" có ghi rõ không liên quan tới Raydium, Phantom hay chính
  phủ nào. Meme coin rủi ro cao.

## Cho người sửa code

```bash
cd launchpad
npm install
npm test           # so khớp với SDK Raydium, kiểm tra upload Arweave, kiểm tra cách gói giao dịch
npm run build      # build src/ thành site/assets/app.js (cần chạy lại sau mỗi lần sửa src/)
npm run serve      # xem thử ở http://localhost:8080
npm run e2e        # chạy thử toàn bộ web trên Chromium với blockchain/ví giả (cần Playwright)
```

```
site/                 web tĩnh, upload thư mục này
  config.js           cấu hình (phí, ví phí, RPC, mạng)
  assets/app.js       code đã build
src/chain/launchlab.js  lệnh LaunchLab, PDA, toán bonding curve (khớp SDK Raydium)
src/chain/launch.js     gói giao dịch: phí + tạo coin + dev buy
src/chain/flow.js       quy trình tạo coin, mua, bán
src/chain/indexer.js    đọc danh sách coin từ lịch sử ví phí
src/upload/arweave.js   lưu ảnh/metadata lên Arweave (ANS-104)
src/ui/                 giao diện
tools/                  build, server thử, test trình duyệt, blockchain giả
```
