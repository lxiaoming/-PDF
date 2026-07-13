#!/usr/bin/env python3
"""
图片转 PDF 工具（跨平台）
- 选择根目录，递归扫描所有包含图片的文件夹
- 每个文件夹的图片按文件名自然序合并为一个 PDF
- PDF 直接保存在对应的图片文件夹内，名称 = 文件夹名
- 依赖：Pillow（图片格式转换）
"""

import os
import re
import struct
import tempfile
import threading
import tkinter as tk
from datetime import datetime
from io import BytesIO
from tkinter import filedialog, messagebox, ttk

from PIL import Image

# ---------------------------------------------------------------------------
# 纯 Python PDF 生成器
# ---------------------------------------------------------------------------

SUPPORTED_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp"}

PAGE_W = 595.0
PAGE_H = 842.0
MARGIN = 20.0


def _natural_sort_key(filename: str) -> tuple:
    parts = re.split(r"(\d+)", filename)
    key: list = []
    for p in parts:
        key.append(int(p) if p.isdigit() else p.lower())
    return tuple(key)


def _get_jpeg_dimensions(filepath: str) -> tuple[int, int]:
    with open(filepath, "rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            raise ValueError(f"不是有效的 JPEG 文件: {filepath}")
        while True:
            marker = fh.read(2)
            if len(marker) < 2:
                raise ValueError("JPEG 文件意外结束")
            if marker[0] != 0xFF:
                continue
            tag = marker[1]
            if tag == 0xFF:
                continue
            if 0xC0 <= tag <= 0xC2:
                length = struct.unpack(">H", fh.read(2))[0]
                fh.read(1)
                h = struct.unpack(">H", fh.read(2))[0]
                w = struct.unpack(">H", fh.read(2))[0]
                return w, h
            elif tag == 0xD8 or tag == 0xD9:
                continue
            else:
                length = struct.unpack(">H", fh.read(2))[0]
                fh.seek(length - 2, 1)


def _convert_to_jpeg(src_path: str, dst_path: str, quality: int = 85) -> None:
    """使用 Pillow 将任意支持的图片格式转为 JPEG（跨平台）"""
    img = Image.open(src_path)
    # RGBA / P 模式无法直接保存为 JPEG，需先转为 RGB
    if img.mode in ("RGBA", "P", "CMYK", "LA"):
        img = img.convert("RGB")
    img.save(dst_path, "JPEG", quality=quality)


def _make_pdf_from_images(
    image_paths: list[str],
    output_pdf_path: str,
    quality: int = 85,
    progress_callback=None,
) -> str:
    temp_dir = tempfile.mkdtemp(prefix="img2pdf_")
    jpeg_paths: list[str] = []
    temp_files: list[str] = []
    total = len(image_paths)

    try:
        for idx, img_path in enumerate(image_paths):
            if progress_callback:
                progress_callback(idx + 1, total, f"准备: {os.path.basename(img_path)}")
            ext = os.path.splitext(img_path)[1].lower()
            if ext in (".jpg", ".jpeg"):
                jpeg_paths.append(img_path)
            else:
                tmp_name = os.path.join(
                    temp_dir,
                    f"{os.path.splitext(os.path.basename(img_path))[0]}.jpg",
                )
                _convert_to_jpeg(img_path, tmp_name, quality)
                jpeg_paths.append(tmp_name)
                temp_files.append(tmp_name)

        pdf_bytes = _build_pdf(jpeg_paths, quality, progress_callback, offset=total)
        with open(output_pdf_path, "wb") as fh:
            fh.write(pdf_bytes)
        return output_pdf_path

    finally:
        for tf in temp_files:
            try:
                os.remove(tf)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass


def _build_pdf(jpeg_paths, quality, progress_callback=None, offset=0):
    pdf = BytesIO()
    objects: list[tuple[int, bytes]] = []

    def obj(id_, content):
        objects.append((id_, content))

    pages_info: list[dict] = []
    for idx, jpg_path in enumerate(jpeg_paths):
        if progress_callback:
            progress_callback(
                offset + idx + 1,
                offset + len(jpeg_paths),
                f"嵌入: {os.path.basename(jpg_path)}",
            )
        w, h = _get_jpeg_dimensions(jpg_path)
        with open(jpg_path, "rb") as fh:
            jpg_data = fh.read()

        avail_w = PAGE_W - 2 * MARGIN
        avail_h = PAGE_H - 2 * MARGIN
        scale = min(avail_w / w, avail_h / h, 1.0)
        draw_w = w * scale
        draw_h = h * scale
        x = (PAGE_W - draw_w) / 2.0
        y = (PAGE_H - draw_h) / 2.0

        pages_info.append({
            "width": w, "height": h,
            "draw_w": draw_w, "draw_h": draw_h,
            "x": x, "y": y, "data": jpg_data,
        })

    next_id = [1]

    def new_id():
        n = next_id[0]; next_id[0] += 1; return n

    catalog_id = new_id()
    pages_id = new_id()

    page_ids, image_ids, content_ids = [], [], []
    for _ in pages_info:
        page_ids.append(new_id())
        image_ids.append(new_id())
        content_ids.append(new_id())

    obj(catalog_id, _catalog(pages_id))
    obj(pages_id, _pages(page_ids))

    for i, info in enumerate(pages_info):
        obj(image_ids[i], _image_xobject(info))
        obj(content_ids[i], _content_stream(image_ids[i], info))
        obj(page_ids[i], _page_obj(page_ids[i], content_ids[i], image_ids[i]))

    pdf.write(b"%PDF-1.4\n%\xff\xff\xff\xff\n")

    offsets: dict[int, int] = {}
    for obj_id, content in objects:
        offsets[obj_id] = pdf.tell()
        pdf.write(f"{obj_id} 0 obj\n".encode())
        pdf.write(content)
        if not content.endswith(b"\n"):
            pdf.write(b"\n")
        pdf.write(b"endobj\n")

    xref_offset = pdf.tell()
    pdf.write(b"xref\n")
    pdf.write(f"0 {len(objects) + 1}\n".encode())
    pdf.write(b"0000000000 65535 f \n")
    for obj_id in sorted(offsets.keys()):
        pdf.write(f"{offsets[obj_id]:010d} 00000 n \n".encode())

    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF"
    )
    pdf.write(trailer.encode())
    return pdf.getvalue()


def _catalog(pages_id):
    return f"<< /Type /Catalog /Pages {pages_id} 0 R >>\n".encode()


def _pages(page_ids):
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    return f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>\n".encode()


def _page_obj(page_id, content_id, image_id):
    return (
        f"<< /Type /Page /Parent 2 0 R\n"
        f"   /MediaBox [0 0 {PAGE_W} {PAGE_H}]\n"
        f"   /Contents {content_id} 0 R\n"
        f"   /Resources << /XObject << /I{image_id} {image_id} 0 R >> >>\n"
        f">>\n"
    ).encode()


def _image_xobject(info):
    return (
        f"<< /Type /XObject /Subtype /Image\n"
        f"   /Width {info['width']} /Height {info['height']}\n"
        f"   /ColorSpace /DeviceRGB\n"
        f"   /BitsPerComponent 8\n"
        f"   /Filter /DCTDecode\n"
        f"   /Length {len(info['data'])}\n"
        f">>\nstream\n"
    ).encode() + info["data"] + b"\nendstream\n"


def _content_stream(image_id, info):
    data = (
        f"q\n  {info['draw_w']:.1f} 0 0 {info['draw_h']:.1f}"
        f" {info['x']:.1f} {info['y']:.1f} cm\n"
        f"  /I{image_id} Do\nQ\n"
    ).encode()
    return f"<< /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream\n"


# ---------------------------------------------------------------------------
# 文件夹扫描
# ---------------------------------------------------------------------------

def scan_images_in_folder(folder_path: str) -> list[str]:
    images: list[str] = []
    try:
        for name in os.listdir(folder_path):
            ext = os.path.splitext(name)[1].lower()
            if ext in SUPPORTED_EXTS:
                images.append(os.path.join(folder_path, name))
    except PermissionError:
        pass
    images.sort(key=lambda p: _natural_sort_key(os.path.basename(p)))
    return images


def scan_all_folders(root_path: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for dirpath, _dirnames, _filenames in os.walk(root_path):
        images = scan_images_in_folder(dirpath)
        if images:
            result[dirpath] = images
    return dict(sorted(result.items()))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class ImageToPdfApp:
    """主界面"""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("图片转 PDF 工具")
        self.root.geometry("740x640")
        self.root.resizable(True, True)
        self.root.minsize(580, 500)

        self.root_path: str = ""
        self.folders: dict[str, list[str]] = {}  # {文件夹路径: [图片路径]}
        self.pdf_history: list[str] = []

        # 输出目录：
        #   默认（未手动选择）：{root_parent}/{root_name}-pdf/
        #   手动选择了目录：    {选中目录}/{root_name}/
        self.output_base: str = ""     # 用户选择的基础目录，空=默认
        self._output_custom: bool = False  # 用户是否手动选择了目录

        self._build_ui()

    def _build_ui(self):
        header = tk.Label(
            self.root,
            text="📄 图片 → PDF 转换工具",
            font=("Helvetica", 16, "bold"),
            pady=10,
        )
        header.pack()

        # ① 选择根目录
        folder_frame = tk.LabelFrame(self.root, text="① 选择根目录", padx=10, pady=8)
        folder_frame.pack(fill=tk.BOTH, padx=16, pady=(2, 8), expand=True)

        btn_bar = tk.Frame(folder_frame)
        btn_bar.pack(fill=tk.X)

        self.btn_select = tk.Button(
            btn_bar, text="📁 选择根目录", command=self._on_select_root
        )
        self.btn_select.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_clear = tk.Button(
            btn_bar, text="🗑 清空列表", command=self._on_clear_folders
        )
        self.btn_clear.pack(side=tk.LEFT)

        self.btn_rescan = tk.Button(
            btn_bar, text="🔄 重新扫描", command=self._on_rescan
        )
        self.btn_rescan.pack(side=tk.LEFT, padx=(8, 0))

        self.lbl_count = tk.Label(btn_bar, text="未选择根目录", fg="gray")
        self.lbl_count.pack(side=tk.RIGHT)

        list_wrapper = tk.Frame(folder_frame)
        list_wrapper.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        scrollbar = tk.Scrollbar(list_wrapper)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.listbox = tk.Listbox(
            list_wrapper,
            selectmode=tk.EXTENDED,
            yscrollcommand=scrollbar.set,
            font=("Menlo", 10),
        )
        self.listbox.pack(fill=tk.BOTH, expand=True)
        self.listbox.bind("<Delete>", lambda e: self._on_delete_selected())
        scrollbar.config(command=self.listbox.yview)

        # ② 设置
        set_frame = tk.LabelFrame(self.root, text="② 设置", padx=10, pady=8)
        set_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        desc_row = tk.Frame(set_frame)
        desc_row.pack(fill=tk.X, pady=(0, 4))

        dir_row = tk.Frame(set_frame)
        dir_row.pack(fill=tk.X)
        tk.Label(dir_row, text="保存到:").pack(side=tk.LEFT)
        self.lbl_output_dir = tk.Label(
            dir_row, text="（选择根目录后自动设置）", fg="#2a6e9b", anchor="w"
        )
        self.lbl_output_dir.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        tk.Button(dir_row, text="浏览…", command=self._on_select_output_dir).pack(
            side=tk.RIGHT
        )

        name_row = tk.Frame(set_frame)
        name_row.pack(fill=tk.X, pady=(4, 0))
        tk.Label(
            name_row,
            text="📌 PDF 保存在 {根目录}-pdf 下，保持原目录层级，名称 = 文件夹名.pdf",
            fg="#888",
        ).pack(side=tk.LEFT)

        quality_row = tk.Frame(set_frame)
        quality_row.pack(fill=tk.X, pady=(4, 0))
        tk.Label(quality_row, text="JPEG 质量:").pack(side=tk.LEFT)
        self.scale_quality = tk.Scale(
            quality_row, from_=10, to=100, orient=tk.HORIZONTAL, length=200,
        )
        self.scale_quality.set(85)
        self.scale_quality.pack(side=tk.LEFT, padx=6)
        self.lbl_quality_val = tk.Label(quality_row, text="85", width=3)
        self.lbl_quality_val.pack(side=tk.LEFT)
        self.scale_quality.config(
            command=lambda v: self.lbl_quality_val.config(text=v)
        )

        # ③ 预览
        preview_frame = tk.LabelFrame(
            self.root, text="③ 将生成的 PDF 预览", padx=10, pady=8
        )
        preview_frame.pack(fill=tk.BOTH, padx=16, pady=(0, 8), expand=False)

        self.text_preview = tk.Text(
            preview_frame, height=4, font=("Menlo", 10), state=tk.DISABLED, fg="#555",
        )
        self.text_preview.pack(fill=tk.BOTH, expand=True)

        # ④ 生成按钮
        btn_frame = tk.Frame(self.root)
        btn_frame.pack(fill=tk.X, padx=16, pady=(0, 8))

        self.btn_generate = tk.Button(
            btn_frame,
            text="🚀 生成 PDF",
            font=("Helvetica", 13, "bold"),
            bg="#4CAF50",
            fg="white",
            activebackground="#45a049",
            height=2,
            command=self._on_generate,
        )
        self.btn_generate.pack(fill=tk.X)

        # ⑤ 历史
        hist_frame = tk.LabelFrame(
            self.root, text="⑤ 已生成的 PDF 历史", padx=10, pady=8
        )
        hist_frame.pack(fill=tk.BOTH, padx=16, pady=(0, 8), expand=True)

        hist_scrollbar = tk.Scrollbar(hist_frame)
        hist_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.text_history = tk.Text(
            hist_frame,
            height=6,
            yscrollcommand=hist_scrollbar.set,
            font=("Menlo", 10),
            state=tk.DISABLED,
        )
        self.text_history.pack(fill=tk.BOTH, expand=True)
        hist_scrollbar.config(command=self.text_history.yview)

        self.progress = ttk.Progressbar(self.root, mode="determinate")
        self.progress.pack(fill=tk.X, padx=16, pady=(0, 4))

        self.lbl_status = tk.Label(self.root, text="就绪", fg="gray", anchor="w")
        self.lbl_status.pack(fill=tk.X, padx=16, pady=(0, 8))

    # -----------------------------------------------------------------------
    # 事件处理
    # -----------------------------------------------------------------------

    def _on_select_root(self):
        folder = filedialog.askdirectory(
            title="选择根目录（会递归扫描所有子文件夹）",
            initialdir=os.path.expanduser("~"),
        )
        if not folder:
            return
        self.root_path = folder
        # 默认：{parent}/{root_name}-pdf
        self.output_base = os.path.dirname(folder.rstrip(os.sep))
        self._output_custom = False
        self.lbl_output_dir.config(text=self._get_output_dir())
        self._do_scan()

    def _on_rescan(self):
        if not self.root_path:
            messagebox.showinfo("提示", "请先选择根目录")
            return
        self._do_scan()

    def _do_scan(self):
        self.folders = scan_all_folders(self.root_path)
        self._refresh_listbox()
        self._update_count_label()
        self._update_preview()

        if not self.folders:
            messagebox.showinfo(
                "未找到图片",
                f"「{os.path.basename(self.root_path)}」及其子文件夹中未找到图片文件。",
            )
            self._update_status("未找到图片")
        else:
            self._update_status(
                f"扫描完成：{os.path.basename(self.root_path)} → {len(self.folders)} 个子文件夹"
            )

    def _on_clear_folders(self):
        self.folders.clear()
        self.listbox.delete(0, tk.END)
        self._clear_preview()
        self._update_count_label()
        self._update_status("已清空列表")

    def _on_delete_selected(self):
        selected = self.listbox.curselection()
        if not selected:
            return
        folder_paths = list(self.folders.keys())
        for idx in reversed(selected):
            del self.folders[folder_paths[idx]]
        self._refresh_listbox()
        self._update_count_label()
        self._update_preview()

    def _get_output_dir(self) -> str:
        """实际输出目录。默认 = {root}-pdf，手动选择后 = {选中目录}/{root_name}/"""
        if not self.root_path:
            return ""
        root_name = os.path.basename(self.root_path.rstrip(os.sep))
        if self._output_custom:
            return os.path.join(self.output_base, root_name)
        else:
            return os.path.join(self.output_base, root_name + "-pdf")

    def _on_select_output_dir(self):
        """手动选择输出基础目录"""
        d = filedialog.askdirectory(
            title="选择保存位置（会创建 {根目录名} 子文件夹）",
            initialdir=self.output_base or os.path.expanduser("~"),
        )
        if d:
            self.output_base = d
            self._output_custom = True
            self.lbl_output_dir.config(text=self._get_output_dir())
            self._update_preview()

    def _on_generate(self):
        if not self.folders:
            messagebox.showwarning("提示", "请先选择根目录并扫描")
            return

        non_empty = {k: v for k, v in self.folders.items() if v}
        if not non_empty:
            messagebox.showwarning("提示", "所有文件夹中都没有图片")
            return

        output_dir = self._get_output_dir()

        # 检查重名
        conflicts = []
        for folder_path in non_empty:
            rel = os.path.relpath(folder_path, self.root_path)
            pdf_name = os.path.basename(folder_path) + ".pdf"
            pdf_path = os.path.join(output_dir, rel, pdf_name)
            if os.path.exists(pdf_path):
                conflicts.append(pdf_path)

        if conflicts:
            msg = "以下文件已存在，将被覆盖：\n\n" + "\n".join(conflicts)
            if not messagebox.askyesno("文件冲突", msg + "\n\n是否继续？"):
                return

        quality = self.scale_quality.get()
        folders_snapshot = {k: list(v) for k, v in non_empty.items()}
        total_images = sum(len(v) for v in folders_snapshot.values())

        self._set_ui_state(tk.DISABLED)
        self.progress["value"] = 0
        self.progress["maximum"] = total_images

        def _run():
            try:
                generated_pdfs: list[str] = []
                processed = 0

                for folder_path, img_paths in folders_snapshot.items():
                    rel = os.path.relpath(folder_path, self.root_path)
                    pdf_name = os.path.basename(folder_path) + ".pdf"
                    pdf_dir = os.path.join(output_dir, rel)
                    os.makedirs(pdf_dir, exist_ok=True)
                    pdf_path = os.path.join(pdf_dir, pdf_name)

                    def make_cb(offset):
                        def cb(cur, tot, msg):
                            self.root.after(
                                0,
                                lambda: self._update_progress(
                                    offset + cur, total_images, msg
                                ),
                            )
                        return cb

                    _make_pdf_from_images(
                        img_paths, pdf_path, quality=quality,
                        progress_callback=make_cb(processed),
                    )
                    generated_pdfs.append(pdf_path)
                    processed += len(img_paths)

                self.root.after(0, lambda: self._on_success(generated_pdfs))
            except Exception as e:
                self.root.after(0, lambda: self._on_error(str(e)))

        threading.Thread(target=_run, daemon=True).start()

    def _update_progress(self, current, total, msg):
        self.progress["value"] = current
        self.progress["maximum"] = total
        self.lbl_status.config(text=msg)

    def _on_success(self, pdf_paths: list[str]):
        self._set_ui_state(tk.NORMAL)
        self.progress["value"] = 0

        ts = datetime.now().strftime("%H:%M:%S")
        self.text_history.config(state=tk.NORMAL)
        for p in pdf_paths:
            self.pdf_history.append(p)
            self.text_history.insert(tk.END, f"[{ts}] ✅ {p}\n")
        self.text_history.see(tk.END)
        self.text_history.config(state=tk.DISABLED)

        count = len(pdf_paths)
        self._update_status(f"成功生成 {count} 个 PDF")
        self.root.lift()
        messagebox.showinfo(
            "生成成功",
            f"共生成 {count} 个 PDF：\n\n" + "\n".join(pdf_paths),
        )

    def _on_error(self, error_msg: str):
        self._set_ui_state(tk.NORMAL)
        self.progress["value"] = 0
        self._update_status("生成失败")

        ts = datetime.now().strftime("%H:%M:%S")
        self.text_history.config(state=tk.NORMAL)
        self.text_history.insert(tk.END, f"[{ts}] ❌ 失败: {error_msg}\n")
        self.text_history.see(tk.END)
        self.text_history.config(state=tk.DISABLED)

        messagebox.showerror("生成失败", f"错误信息：\n{error_msg}")

    # -----------------------------------------------------------------------
    # 辅助方法
    # -----------------------------------------------------------------------

    def _refresh_listbox(self):
        self.listbox.delete(0, tk.END)
        for folder_path, images in self.folders.items():
            if self.root_path and folder_path.startswith(self.root_path):
                display = os.path.relpath(folder_path, self.root_path)
            else:
                display = os.path.basename(folder_path)
            self.listbox.insert(
                tk.END, f"📁 {display}  ({len(images)} 张图片)"
            )

    def _update_count_label(self):
        total_folders = len(self.folders)
        total_images = sum(len(v) for v in self.folders.values())
        if total_folders == 0:
            self.lbl_count.config(text="未选择根目录")
        else:
            self.lbl_count.config(
                text=f"根目录: {os.path.basename(self.root_path)} → "
                     f"{total_folders} 个子文件夹，{total_images} 张图片"
            )

    def _update_preview(self):
        self.text_preview.config(state=tk.NORMAL)
        self.text_preview.delete("1.0", tk.END)
        if not self.folders:
            self._clear_preview()
            return

        output_dir = self._get_output_dir()
        # 预览显示的根名称：默认 -pdf 后缀，手动选择则不加
        root_name = os.path.basename(self.root_path.rstrip(os.sep))
        root_display = root_name if self._output_custom else root_name + "-pdf"

        self.text_preview.insert(
            tk.END, f"📂 输出目录: {output_dir}\n"
        )

        for folder_path, images in self.folders.items():
            if not images:
                continue
            folder_name = os.path.basename(folder_path)
            rel = os.path.relpath(folder_path, self.root_path)
            pdf_path = os.path.join(output_dir, rel, folder_name + ".pdf")
            display = os.path.join(root_display, rel, folder_name + ".pdf")
            marker = " ⚠️ 将覆盖" if os.path.exists(pdf_path) else ""
            self.text_preview.insert(
                tk.END,
                f"📄 {display}  ← {len(images)} 张图片{marker}\n",
            )
        self.text_preview.config(state=tk.DISABLED)

    def _clear_preview(self):
        self.text_preview.config(state=tk.NORMAL)
        self.text_preview.delete("1.0", tk.END)
        self.text_preview.config(state=tk.DISABLED)

    def _update_status(self, text: str):
        self.lbl_status.config(text=text)

    def _set_ui_state(self, state: str):
        for widget in (self.btn_select, self.btn_clear, self.btn_rescan,
                       self.btn_generate, self.listbox):
            widget.config(state=state)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main():
    root = tk.Tk()
    ImageToPdfApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
