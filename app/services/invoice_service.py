"""
Invoice Service — professional PDF invoices for orders (Module 5.6).

Uses ReportLab (platypus) to render a branded, itemized invoice to an in-memory
PDF. ReportLab is an optional import so the app still boots without it; the
invoice endpoints surface a clear 503 when it isn't installed.
"""
from datetime import datetime
from io import BytesIO

from loguru import logger

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    REPORTLAB_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    REPORTLAB_AVAILABLE = False
    logger.warning("reportlab not installed. PDF invoice generation disabled.")


# Brand palette (matches the SRIBEES magenta used across the apps).
_BRAND = "#D81B60"
_INK = "#1F1B24"
_MUTED = "#6B6B76"
_LINE = "#E2DFE6"
_FILL = "#FBF3F7"


def _money(value) -> str:
    try:
        return f"Rs. {float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "Rs. 0.00"


def _pretty_status(order_or_status) -> str:
    if isinstance(order_or_status, str):
        status = order_or_status
        return (status or "").replace("_", " ").title()
    order = order_or_status
    status = getattr(order, "status", "") or ""
    if status == "return_rejected":
        return "Return Rejected"
    elif status == "return_requested":
        return "Return Requested"
    elif status in ("return_approved", "refunded"):
        # Check if partial
        items = getattr(order, "items", []) or []
        ret_items = getattr(order, "return_items", None)
        if ret_items and isinstance(ret_items, list) and len(ret_items) > 0 and items:
            total_items_qty = sum(getattr(it, "quantity", 1) for it in items)
            ret_qty = sum(int(r.get("quantity", 0) or 0) for r in ret_items if isinstance(r, dict))
            if 0 < ret_qty < total_items_qty:
                prefix = "Refunded" if status == "refunded" else "Return Approved"
                return f"{prefix} (Partial)"
        return "Refunded" if status == "refunded" else "Return Approved"
    return (status or "").replace("_", " ").title()


class InvoiceService:
    """Renders order invoices to PDF."""

    @staticmethod
    def generate_invoice_pdf(order, customer=None) -> bytes:
        """
        Build a PDF invoice for *order* and return the raw bytes.

        ``order.items`` and ``order.delivery_address`` must be loaded.
        ``customer`` is a User-like object (full_name/email/phone); if omitted,
        ``order.user`` is used when available.
        """
        if not REPORTLAB_AVAILABLE:
            raise RuntimeError("PDF generation is unavailable (reportlab not installed).")

        customer = customer or getattr(order, "user", None)

        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=18 * mm,
            rightMargin=18 * mm,
            topMargin=16 * mm,
            bottomMargin=16 * mm,
            title=f"Invoice {order.order_number}",
        )

        styles = getSampleStyleSheet()
        brand = colors.HexColor(_BRAND)
        ink = colors.HexColor(_INK)
        muted = colors.HexColor(_MUTED)

        h_brand = ParagraphStyle(
            "Brand", parent=styles["Title"], fontSize=22, textColor=brand,
            spaceAfter=0, leading=24,
        )
        h_invoice = ParagraphStyle(
            "InvoiceLabel", parent=styles["Normal"], fontSize=18, textColor=ink,
            alignment=TA_RIGHT, fontName="Helvetica-Bold",
        )
        label = ParagraphStyle(
            "Label", parent=styles["Normal"], fontSize=8, textColor=muted,
            fontName="Helvetica-Bold", spaceAfter=2,
        )
        body = ParagraphStyle("Body", parent=styles["Normal"], fontSize=9.5, textColor=ink, leading=13)
        body_right = ParagraphStyle("BodyRight", parent=body, alignment=TA_RIGHT)
        cell = ParagraphStyle("Cell", parent=styles["Normal"], fontSize=9, textColor=ink, leading=12)
        cell_right = ParagraphStyle("CellRight", parent=cell, alignment=TA_RIGHT)

        elements = []

        # ---- Header: brand + INVOICE ------------------------------------
        header = Table(
            [[Paragraph("SRIBEES<font color='%s'>online</font>" % _INK, h_brand),
              Paragraph("INVOICE", h_invoice)]],
            colWidths=[95 * mm, 79 * mm],
        )
        header.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        elements.append(header)
        elements.append(Paragraph("Fresh groceries, delivered.", ParagraphStyle(
            "Tagline", parent=body, textColor=muted, fontSize=9)))
        elements.append(Spacer(1, 6 * mm))
        elements.append(Table([[""]], colWidths=[174 * mm], style=TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 1.4, brand),
        ])))
        elements.append(Spacer(1, 5 * mm))

        # ---- Order meta -------------------------------------------------
        created = order.created_at
        created_str = (
            created.strftime("%b %d, %Y %H:%M") if isinstance(created, datetime) else "—"
        )
        meta = Table(
            [
                [Paragraph("ORDER", label), Paragraph("DATE", label), Paragraph("STATUS", label)],
                [
                    Paragraph(order.order_number, body),
                    Paragraph(created_str, body),
                    Paragraph(_pretty_status(order), body),
                ],
            ],
            colWidths=[58 * mm, 58 * mm, 58 * mm],
        )
        meta.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(_FILL)),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(_LINE)),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(meta)
        elements.append(Spacer(1, 5 * mm))

        # ---- Bill To / Deliver To --------------------------------------
        cust_name = getattr(customer, "full_name", None) or "Customer"
        cust_email = getattr(customer, "email", None) or ""
        cust_phone = getattr(customer, "phone", None) or ""
        bill_lines = [Paragraph("BILL TO", label), Paragraph(cust_name, body)]
        if cust_email:
            bill_lines.append(Paragraph(cust_email, body))
        if cust_phone:
            bill_lines.append(Paragraph(cust_phone, body))

        addr = getattr(order, "delivery_address", None)
        if addr is not None:
            addr_text = ", ".join(
                str(p) for p in [
                    addr.address_line1, addr.address_line2, addr.postal_city,
                    addr.district, addr.province, addr.postal_code,
                ] if p
            )
        else:
            addr_text = "—"
        deliver_lines = [Paragraph("DELIVER TO", label), Paragraph(addr_text, body)]

        bill_to = Table([[c] for c in bill_lines], colWidths=[85 * mm])
        deliver_to = Table([[c] for c in deliver_lines], colWidths=[85 * mm])
        for t in (bill_to, deliver_to):
            t.setStyle(TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]))
        parties = Table([[bill_to, deliver_to]], colWidths=[87 * mm, 87 * mm])
        parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        elements.append(parties)
        elements.append(Spacer(1, 6 * mm))

        # ---- Items table -----------------------------------------------
        head_style = ParagraphStyle(
            "Th", parent=cell, textColor=colors.white, fontName="Helvetica-Bold", fontSize=8.5)
        head_right = ParagraphStyle("ThR", parent=head_style, alignment=TA_RIGHT)

        rows = [[
            Paragraph("Product", head_style),
            Paragraph("Variant / SKU", head_style),
            Paragraph("Qty", head_right),
            Paragraph("Unit Price", head_right),
            Paragraph("Total", head_right),
        ]]

        # Parse return items map if order was approved/refunded or in return review
        returned_qty_map = {}
        raw_ret_items = getattr(order, "return_items", None)
        is_return_active = getattr(order, "status", "") in ("return_approved", "refunded")
        if raw_ret_items and isinstance(raw_ret_items, list):
            for r in raw_ret_items:
                if isinstance(r, dict):
                    i_id = str(r.get("order_item_id") or r.get("item_id") or r.get("id") or "")
                    q = int(r.get("quantity") or 0)
                    if i_id and q > 0:
                        returned_qty_map[i_id] = q
        elif is_return_active and raw_ret_items is None:
            # Full return of all items
            for it in (order.items or []):
                returned_qty_map[str(getattr(it, "id", ""))] = getattr(it, "quantity", 1)

        for it in (order.items or []):
            it_id_str = str(getattr(it, "id", ""))
            ret_qty = returned_qty_map.get(it_id_str, 0)

            prod_title = it.product_name or "—"
            qty_text = str(it.quantity)

            if is_return_active and ret_qty > 0:
                if ret_qty >= it.quantity:
                    prod_title += "<br/><font size='7.5' color='#C62828'><b>[Item Returned]</b></font>"
                    qty_text = f"{it.quantity}<br/><font size='7' color='#C62828'>(-{ret_qty})</font>"
                else:
                    prod_title += f"<br/><font size='7.5' color='#C62828'><b>[Returned: {ret_qty} of {it.quantity}]</b></font>"
                    qty_text = f"{it.quantity}<br/><font size='7' color='#C62828'>(-{ret_qty})</font>"

            rows.append([
                Paragraph(prod_title, cell),
                Paragraph(it.product_sku or "—", cell),
                Paragraph(qty_text, cell_right),
                Paragraph(_money(it.unit_price), cell_right),
                Paragraph(_money(it.subtotal), cell_right),
            ])
        if len(rows) == 1:
            rows.append([Paragraph("No items on this order", cell), "", "", "", ""])

        items_table = Table(rows, colWidths=[62 * mm, 44 * mm, 14 * mm, 27 * mm, 27 * mm], repeatRows=1)
        items_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), brand),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor(_LINE)),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FCFAFB")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(items_table)
        elements.append(Spacer(1, 5 * mm))

        # ---- Totals -----------------------------------------------------
        def total_row(name, value, bold=False, color=None):
            name_style = ParagraphStyle(
                "TotN", parent=body_right,
                fontName="Helvetica-Bold" if bold else "Helvetica",
                textColor=color or ink, fontSize=11 if bold else 9.5,
            )
            val_style = ParagraphStyle(
                "TotV", parent=body_right,
                fontName="Helvetica-Bold" if bold else "Helvetica",
                textColor=color or ink, fontSize=11 if bold else 9.5,
            )
            return [Paragraph(name, name_style), Paragraph(value, val_style)]

        totals_data = [
            total_row("Subtotal", _money(order.subtotal)),
            total_row("Delivery Fee", _money(order.shipping_amount)),
        ]
        if float(order.tax_amount or 0) > 0:
            totals_data.append(total_row("Tax", _money(order.tax_amount)))
        if float(order.discount_amount or 0) > 0:
            totals_data.append(total_row("Discount", f"- {_money(order.discount_amount)}",
                                         color=colors.HexColor("#2E7D32")))
        if float(getattr(order, "wallet_deduction", 0) or 0) > 0:
            totals_data.append(total_row("Wallet Applied", f"- {_money(order.wallet_deduction)}",
                                         color=brand))

        refund_amt = float(getattr(order, "refund_amount", 0) or 0)
        orig_total = float(order.total_amount or 0)

        if is_return_active and refund_amt > 0:
            totals_data.append(total_row("Original Total", _money(orig_total)))
            totals_data.append(total_row("Less: Refunded Amount", f"- {_money(refund_amt)}",
                                         color=colors.HexColor("#C62828")))
            net_total = max(0.0, orig_total - refund_amt)
            totals_data.append(total_row("Net Order Total", _money(net_total), bold=True, color=brand))
        else:
            totals_data.append(total_row("Total", _money(orig_total), bold=True, color=brand))

        totals = Table(totals_data, colWidths=[52 * mm, 32 * mm])
        totals.setStyle(TableStyle([
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.HexColor(_LINE)),
        ]))
        # Right-align the totals block.
        totals_wrap = Table([[totals]], colWidths=[174 * mm])
        totals_wrap.setStyle(TableStyle([("ALIGN", (0, 0), (-1, -1), "RIGHT")]))
        elements.append(totals_wrap)
        elements.append(Spacer(1, 6 * mm))

        # ---- Return / Refund / Rejection Notice Box ---------------------
        order_status = getattr(order, "status", "")
        if order_status in ("return_approved", "refunded"):
            res = getattr(order, "return_resolution", "refund") or "refund"
            res_note = getattr(order, "return_resolution_note", "") or ""
            note_content = [
                Paragraph(f"<b>RETURN RESOLUTION: {res.upper()}</b>", ParagraphStyle(
                    "RetNoticeTitle", parent=body, textColor=colors.HexColor("#1B5E20"), fontName="Helvetica-Bold", fontSize=9.5
                )),
                Spacer(1, 1.5 * mm),
                Paragraph(
                    f"This order has processed return resolution ({res}). Refund amount: <b>{_money(refund_amt)}</b>.",
                    ParagraphStyle("RetNoticeBody", parent=body, textColor=colors.HexColor("#2E7D32"), fontSize=8.5)
                ),
            ]
            if res_note:
                note_content.append(Spacer(1, 1 * mm))
                note_content.append(Paragraph(
                    f"<b>Decision Note:</b> {res_note}",
                    ParagraphStyle("RetNoticeNote", parent=body, textColor=colors.HexColor("#1B5E20"), fontSize=8.5)
                ))
            notice_box = Table([[note_content]], colWidths=[174 * mm])
            notice_box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#E8F5E9")),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#81C784")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]))
            elements.append(notice_box)
            elements.append(Spacer(1, 5 * mm))

        elif order_status == "return_rejected":
            rej_reason = getattr(order, "return_reason", "") or "Customer Return"
            rej_note = getattr(order, "return_resolution_note", "") or "Claim did not meet return policy criteria."
            rej_content = [
                Paragraph("<b>RETURN CLAIM REJECTED</b>", ParagraphStyle(
                    "RejNoticeTitle", parent=body, textColor=colors.HexColor("#B71C1C"), fontName="Helvetica-Bold", fontSize=9.5
                )),
                Spacer(1, 1.5 * mm),
                Paragraph(
                    f"Return reason requested: <b>{rej_reason.replace('_', ' ').title()}</b>. The claim was reviewed and declined by admin.",
                    ParagraphStyle("RejNoticeBody", parent=body, textColor=colors.HexColor("#C62828"), fontSize=8.5)
                ),
                Spacer(1, 1 * mm),
                Paragraph(
                    f"<b>Admin Decision Note:</b> {rej_note}",
                    ParagraphStyle("RejNoticeNote", parent=body, textColor=colors.HexColor("#B71C1C"), fontSize=8.5)
                ),
            ]
            rej_box = Table([[rej_content]], colWidths=[174 * mm])
            rej_box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFEBEE")),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#E57373")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]))
            elements.append(rej_box)
            elements.append(Spacer(1, 5 * mm))

        elif order_status == "return_requested":
            ret_reason = getattr(order, "return_reason", "") or "Return Requested"
            req_content = [
                Paragraph("<b>RETURN CLAIM PENDING REVIEW</b>", ParagraphStyle(
                    "ReqNoticeTitle", parent=body, textColor=colors.HexColor("#E65100"), fontName="Helvetica-Bold", fontSize=9.5
                )),
                Spacer(1, 1.5 * mm),
                Paragraph(
                    f"Customer submitted a return request (Reason: <b>{ret_reason.replace('_', ' ').title()}</b>). "
                    "This claim is currently pending administrative verification.",
                    ParagraphStyle("ReqNoticeBody", parent=body, textColor=colors.HexColor("#EF6C00"), fontSize=8.5)
                ),
            ]
            req_box = Table([[req_content]], colWidths=[174 * mm])
            req_box.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF8E1")),
                ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#FFE082")),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]))
            elements.append(req_box)
            elements.append(Spacer(1, 5 * mm))

        # ---- Footer -----------------------------------------------------
        elements.append(Table([[""]], colWidths=[174 * mm], style=TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor(_LINE)),
        ])))
        elements.append(Spacer(1, 3 * mm))
        elements.append(Paragraph(
            "Thank you for shopping with SRIBEESonline. This is a system-generated "
            "invoice and needs no signature.",
            ParagraphStyle("Footer", parent=body, textColor=muted, fontSize=8.5),
        ))

        doc.build(elements)
        pdf = buffer.getvalue()
        buffer.close()
        logger.info(f"Invoice PDF generated for order {order.order_number} ({len(pdf)} bytes)")
        return pdf

    @staticmethod
    def filename_for(order) -> str:
        return f"invoice_{order.order_number}.pdf"

    @staticmethod
    def generate_dispatch_manifest_pdf(orders, branch_name: str = "All Branches") -> bytes:
        """
        Build a PDF Courier Dispatch Manifest for a list of *orders* and return raw bytes.
        """
        if not REPORTLAB_AVAILABLE:
            raise RuntimeError("PDF generation is unavailable (reportlab not installed).")

        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            leftMargin=14 * mm,
            rightMargin=14 * mm,
            topMargin=14 * mm,
            bottomMargin=14 * mm,
            title=f"Dispatch Manifest - {branch_name}",
        )

        styles = getSampleStyleSheet()
        brand = colors.HexColor(_BRAND)
        ink = colors.HexColor(_INK)
        muted = colors.HexColor(_MUTED)

        h_title = ParagraphStyle(
            "MTitle", parent=styles["Title"], fontSize=18, textColor=brand, spaceAfter=0, leading=20, fontName="Helvetica-Bold",
        )
        h_sub = ParagraphStyle(
            "MSub", parent=styles["Normal"], fontSize=12, textColor=ink, alignment=TA_RIGHT, fontName="Helvetica-Bold",
        )
        label = ParagraphStyle(
            "MLabel", parent=styles["Normal"], fontSize=8, textColor=muted, fontName="Helvetica-Bold", spaceAfter=2,
        )
        body = ParagraphStyle("MBody", parent=styles["Normal"], fontSize=8.5, textColor=ink, leading=11)
        body_bold = ParagraphStyle("MBodyB", parent=body, fontName="Helvetica-Bold")
        cell_right = ParagraphStyle("MCellR", parent=body, alignment=TA_RIGHT)

        elements = []

        # ---- Header -----------------------------------------------------
        header = Table(
            [[Paragraph("SRIBEES<font color='%s'>online</font>" % _INK, h_title),
              Paragraph("DISPATCH MANIFEST", h_sub)]],
            colWidths=[100 * mm, 82 * mm],
        )
        header.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        elements.append(header)
        elements.append(Paragraph("Warehouse & Courier Handover Summary", ParagraphStyle("SubTag", parent=body, textColor=muted, fontSize=8.5)))
        elements.append(Spacer(1, 4 * mm))

        # ---- Meta Bar ---------------------------------------------------
        total_value = sum(float(getattr(o, "total_amount", 0) or 0) for o in orders)
        cod_orders = [o for o in orders if getattr(o, "payment_method", "") in ("cash_on_delivery", "CASH_ON_DELIVERY")]
        cod_value = sum(float(getattr(o, "total_amount", 0) or 0) for o in cod_orders)
        now_str = datetime.now().strftime("%b %d, %Y %H:%M")

        meta = Table(
            [
                [Paragraph("BRANCH", label), Paragraph("GENERATED", label), Paragraph("TOTAL ORDERS", label), Paragraph("TOTAL VALUE", label), Paragraph("COD COLLECTION", label)],
                [
                    Paragraph(branch_name, body_bold),
                    Paragraph(now_str, body),
                    Paragraph(str(len(orders)), body_bold),
                    Paragraph(_money(total_value), body_bold),
                    Paragraph(_money(cod_value), body_bold),
                ],
            ],
            colWidths=[38 * mm, 36 * mm, 28 * mm, 40 * mm, 40 * mm],
        )
        meta.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(_FILL)),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor(_LINE)),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        elements.append(meta)
        elements.append(Spacer(1, 5 * mm))

        # ---- Orders Table -----------------------------------------------
        head_style = ParagraphStyle("ThM", parent=body, textColor=colors.white, fontName="Helvetica-Bold", fontSize=8)
        head_right = ParagraphStyle("ThMR", parent=head_style, alignment=TA_RIGHT)

        table_rows = [[
            Paragraph("Order #", head_style),
            Paragraph("Recipient & Contact", head_style),
            Paragraph("Delivery Address / Post Office", head_style),
            Paragraph("Items", head_right),
            Paragraph("Total (LKR)", head_right),
            Paragraph("Payment", head_style),
        ]]

        for o in orders:
            cust = getattr(o, "user", None)
            cust_name = getattr(cust, "full_name", None) or getattr(o, "customer_name", "Customer")
            cust_phone = getattr(cust, "phone", None) or getattr(o, "customer_phone", "")
            contact_str = f"<b>{cust_name}</b><br/>{cust_phone}" if cust_phone else f"<b>{cust_name}</b>"

            addr = getattr(o, "delivery_address", None)
            if addr:
                pc = getattr(addr, "postal_city", "") or ""
                dist = getattr(addr, "district", "") or ""
                l1 = getattr(addr, "address_line1", "") or ""
                addr_str = f"{l1}, {pc} ({dist})" if pc else l1
            else:
                addr_str = getattr(o, "address_summary", "—")

            # Orders store "COD" (PaymentMethodEnum). Matching only the
            # "cash_on_delivery" spelling printed every COD parcel on the
            # manifest as PREPAID — a rider reading it would not collect cash.
            is_cod = (getattr(o, "payment_method", "") or "").upper() in ("COD", "CASH_ON_DELIVERY")
            pm_str = "COD" if is_cod else "PREPAID"
            p_status = getattr(o, "payment_status", "pending").upper()
            pay_str = f"<b>{pm_str}</b> ({p_status})"

            items_cnt = len(getattr(o, "items", []) or []) or getattr(o, "item_count", 1)

            table_rows.append([
                Paragraph(getattr(o, "order_number", "—"), body_bold),
                Paragraph(contact_str, body),
                Paragraph(addr_str, body),
                Paragraph(str(items_cnt), cell_right),
                Paragraph(_money(getattr(o, "total_amount", 0)), cell_right),
                Paragraph(pay_str, body),
            ])

        if len(table_rows) == 1:
            table_rows.append([Paragraph("No orders found for dispatch", body), "", "", "", "", ""])

        manifest_table = Table(table_rows, colWidths=[36 * mm, 38 * mm, 52 * mm, 14 * mm, 24 * mm, 18 * mm], repeatRows=1)
        manifest_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), brand),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("LINEBELOW", (0, 1), (-1, -1), 0.3, colors.HexColor(_LINE)),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FCFAFB")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(manifest_table)
        elements.append(Spacer(1, 8 * mm))

        # ---- Handover Signature Block ----------------------------------
        sig_left = Table([
            [Paragraph("<b>Warehouse Dispatcher:</b>", body)],
            [Spacer(1, 10 * mm)],
            [Paragraph("Signature: ______________________", body)],
            [Paragraph("Name: ________________________", body)],
        ], colWidths=[85 * mm])

        sig_right = Table([
            [Paragraph("<b>Courier Handover Agent:</b>", body)],
            [Spacer(1, 10 * mm)],
            [Paragraph("Signature: ______________________", body)],
            [Paragraph("Driver / Agent Name: _____________", body)],
        ], colWidths=[85 * mm])

        for t in (sig_left, sig_right):
            t.setStyle(TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]))

        signatures = Table([[sig_left, sig_right]], colWidths=[91 * mm, 91 * mm])
        signatures.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        elements.append(signatures)

        doc.build(elements)
        pdf = buffer.getvalue()
        buffer.close()
        logger.info(f"Dispatch Manifest PDF generated for {len(orders)} orders ({len(pdf)} bytes)")
        return pdf
