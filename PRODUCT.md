# Ruang Putar

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

HTML, CSS, dan JavaScript tanpa framework. Server Python lokal berbasis pustaka standar menyajikan frontend dan proxy HLS/MP4; yt-dlp dengan runtime Node.js menyiapkan stream. HLS native atau HLS.js 1.7.3 dipakai saat fallback. Tanpa deployment.

## Users

Pengguna pribadi yang ingin menyimpan link YouTube dan menonton di satu halaman.

## Product Purpose

Menambahkan link, memperoleh judul asli dan thumbnail jika tersedia, menyimpan koleksi di browser, dan memutar pilihan melalui embed resmi tanpa navigasi halaman oleh app.

## Capabilities and Constraints

localStorage per browser/origin; format koleksi lama dipertahankan. Pemutar resmi beserta branding dan kontrol YouTube dicoba dahulu. Kegagalan teknis memicu fallback backend lokal dengan satu ekstraksi ulang untuk kegagalan sementara, dengan play/pause, volume, fullscreen, dan seek. HLS non-DRM H.264 dicoba dahulu, lalu MP4 gabungan H.264/AAC hingga 720p; live mengikuti rentang DVR. Tidak ada jaminan semua video dapat diputar. Metadata tetap memakai oEmbed tanpa API key; jika gagal, simpan judul berdasarkan ID. Tidak ada login, sinkronisasi, download penuh, cache video disk, atau playlist. Server hanya loopback pada port 5173, menggunakan origin lama agar koleksi tetap tersedia.

## Brand Commitments

UI sederhana, sudut siku, outline tipis dan tegas, latar lembut. Bahasa Indonesia. Nama Ruang Putar merupakan nama implementasi.
