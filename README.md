# Ruang Putar

Web app sederhana untuk menyimpan link YouTube dan menonton di halaman yang sama. Player resmi dicoba terlebih dahulu. Jika gagal secara teknis, app otomatis mencoba streaming HLS melalui backend lokal berbasis yt-dlp, dengan dukungan seek.

## Jalankan

Kebutuhan: **Python 3.11+**, **Node.js 22+**, dan **yt-dlp** yang mutakhir pada `PATH`. Tidak ada paket Python server tambahan atau proses build frontend. Untuk dukungan YouTube lengkap, instalasi yt-dlp juga perlu komponen `yt-dlp-ejs`; ikuti [panduan resmi EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS). Server tidak memasang atau memperbarui dependensi secara otomatis.

Periksa dependensi:

```sh
python3 --version
node --version
yt-dlp --version
```

Dari folder proyek, hentikan server statis lama pada port 5173 jika masih berjalan, lalu:

```sh
python3 server.py
```

Buka **http://127.0.0.1:5173**. Server hanya mendengarkan pada loopback dan tidak mengganti port otomatis. Gunakan alamat dan port yang sama setiap kali agar koleksi localStorage tetap tersedia; `localhost:5173` dan `127.0.0.1:5173` memiliki koleksi terpisah. Data lama tidak memerlukan migrasi.

`file://` atau `python3 -m http.server` tidak menyediakan API fallback. Jika yt-dlp/Node.js belum tersedia, server tetap menyajikan app dan player resmi; pesan dependensi ditampilkan saat fallback diperlukan.

## Penggunaan

Tempel link video, klik **Simpan video**, lalu **Play** pada koleksi. Mendukung URL YouTube `watch`, `youtu.be`, `shorts`, `live`, dan `embed`. Parameter playlist dan timestamp tidak digunakan. Link ke video yang sama tidak disimpan dua kali. Tombol **Hapus** meminta konfirmasi dan menghentikan video jika sedang dipilih.

Judul dan thumbnail diambil melalui YouTube oEmbed dengan batas tunggu 8 detik. Jika metadata gagal, link tetap disimpan dengan judul berdasarkan ID video. Thumbnail yang gagal dimuat diganti teks. Video baru tidak diputar otomatis saat disimpan.

## Penyimpanan dan batasan

- Key localStorage: `personal-yt-stream:v1`. Nilainya array objek `{ id, url, title, thumbnailUrl }`, terbaru di awal. Hanya metadata/link yang disimpan, bukan file video atau thumbnail.
- Koleksi khusus browser dan origin yang sama. Menghapus data situs menghapus koleksi. Tidak ada akun, sinkronisasi, ekspor, atau download video.
- Jika data lokal tidak dapat dibaca, app tidak menimpanya. Cadangkan nilai localStorage melalui DevTools sebelum memperbaiki atau menghapus key yang rusak. Jika penyimpanan gagal, app tidak melaporkan penyimpanan berhasil.
- Internet diperlukan untuk metadata, thumbnail, dan pemutaran. Endpoint metadata dapat berubah atau diblokir oleh browser/jaringan.
- Pemutar resmi menggunakan `youtube-nocookie.com`, dengan branding dan kontrol YouTube. App tidak membuat redirect saat Play ditekan; tautan di dalam pemutar mengikuti perilaku YouTube.
- Browser dapat menahan autoplay; gunakan Play bawaan pemutar. Pemblokir konten atau kebijakan referrer dapat menghalangi YouTube.

## Cara kerja fallback

- Fallback dicoba satu kali per pilihan video untuk error player selain `2` (input invalid) dan `100` (video tidak tersedia/privat), kegagalan memuat API, player belum siap setelah 15 detik, atau buffering 30 detik saat pemutaran diminta. Autoplay diblokir, pause, dan video selesai tidak memicu fallback.
- Backend mengekstrak metadata stream dengan batas 45 detik, tanpa konfigurasi pengguna, login/cookies akun, atau cache disk. Stream tidak diunduh penuh. Hanya HLS non-DRM H.264 hingga 720p yang didukung; audio dan video disalurkan melalui proxy lokal yang sama.
- Browser dengan MediaSource memakai HLS.js 1.7.3, dimuat lokal hanya ketika diperlukan. HLS native dipakai bila MediaSource tidak didukung dan browser menyediakan HLS native. Pemilihan ini menghindari kegagalan seek HLS native Chromium pada rekaman panjang. Kontrol HTML5 menyediakan play/pause, volume, fullscreen, dan seek. Seek siaran langsung mengikuti rentang DVR yang tersedia.
- URL stream bertanda tangan disimpan hanya dalam sesi server, bukan koleksi localStorage. Semua sumber harus berupa CDN YouTube HTTPS. API tidak menerima URL proxy bebas.
- Sesi dilepas saat video diganti/dihapus atau halaman ditutup; sesi yang tertinggal dibersihkan setelah 30 menit tanpa aktivitas. Tidak ada penyimpanan video ke disk.
- Ketika sumber menolak akses dengan `403/410` atau sesi kedaluwarsa, app mencoba membuat sesi baru sekali dan melanjutkan posisi. HLS native tidak mengekspos HTTP status; network error native mendapat satu percobaan pembaruan yang sama. Jika tetap gagal, tombol **Coba lagi** mengulang dari player resmi.
- Video privat, dihapus, dibatasi wilayah/usia, tidak menyediakan HLS, atau ditolak YouTube tetap dapat gagal. Tidak ada DASH, FFmpeg, transcoding, download penuh, atau layanan proxy publik sebagai fallback lanjutan.
- Ekstraksi dan penyaluran ulang konten perlu mempertimbangkan izin pemilik serta [ketentuan YouTube](https://www.youtube.com/static?template=terms). Keberhasilan teknis bukan izin penggunaan.

## API lokal

| Endpoint | Fungsi |
| --- | --- |
| `POST /api/playback` | Body JSON `{ "videoId": "…" }`; sukses `201` dengan `{ sessionId, manifestUrl }`. |
| `GET /api/playback/{sessionId}/manifest.m3u8` | Master HLS dengan sumber diganti alamat proxy lokal. |
| `GET /api/playback/{sessionId}/resource/{resourceId}` | Playlist/segmen terdaftar; mendukung byte Range dan respons `206/416`. |
| `DELETE /api/playback/{sessionId}` | Lepaskan sesi; idempotent. |

Error berbentuk `{ code, message }`: `400` input invalid, `403` origin/host/sumber ditolak, `404` resource tidak ditemukan, `410` sesi kedaluwarsa, `415` format tidak didukung, `502` ekstraksi/sumber gagal, `503` dependensi tidak tersedia atau server sibuk, `504` ekstraksi timeout. Maksimal dua ekstraksi bersamaan dan 16 sesi aktif. Backend hanya menyajikan file frontend dan `assets/`, bukan source server atau direktori proyek.

## Verifikasi

```sh
python3 -m unittest discover -s tests -v
node --check app.js
```

Tes backend memakai fixture jaringan dan extractor; playback YouTube nyata harus diperiksa terpisah karena akses dapat berubah. Font DM Sans (SIL Open Font License) dan HLS.js 1.7.3 (Apache-2.0) disimpan lokal beserta lisensinya pada `assets/`.
