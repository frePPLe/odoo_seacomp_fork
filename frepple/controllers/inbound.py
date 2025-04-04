# -*- coding: utf-8 -*-
#
# Copyright (C) 2014 by frePPLe bv
#
# This library is free software; you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public
# License along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
import odoo
import logging
from xml.etree.cElementTree import iterparse
from datetime import datetime
from pytz import timezone

logger = logging.getLogger(__name__)


class importer(object):
    def __init__(self, req, database=None, company=None, mode=1):
        self.env = req.env
        self.database = database
        self.company = company
        self.datafile = req.httprequest.files.get("frePPLe plan")

        # The mode argument defines different types of runs:
        #  - Mode 1:
        #    Export of the complete plan. This first erase all previous frePPLe
        #    proposals in draft state.
        #  - Mode 2:
        #    Incremental export of some proposed transactions from frePPLe.
        #    In this mode mode we are not erasing any previous proposals.
        self.mode = int(mode)

    def run(self):
        msg = []

        mfg_order = self.env["mrp.production"]
        logger.info("self.mode is %s" % self.mode)
        if self.mode == 1:
            # Cancel draft delivery plan records
            self.env.cr.execute(
                """
                delete from pdp where status_id = 1
                """
            )
            msg.append(
                "Removed %s old draft delivery plan lines" % self.env.cr.rowcount
            )
            logger.info(
                "Removed %s old draft delivery plan lines" % self.env.cr.rowcount
            )
        # Parsing the XML data file
        countproc = 0
        countmfg = 0

        # Preprocessing step 1
        # load upfront all POs with their supplier_id and their wh_tracking_no
        # Required on full export only
        po_info_dict = {}
        if self.mode == 1:
            m = self.env["purchase.order"]
            recs = m.search([("state", "=", "draft")])
            fields = ["id", "mh_tracking_no", "partner_id"]
            for i in recs.read(fields):
                po_info_dict[(i["mh_tracking_no"], i["partner_id"][0])] = i["id"]

        # Preprocessing step 2
        # load upfront all sales order name and id
        # Required on full export only
        sale_order_dict = {}
        if self.mode == 1:
            m = self.env["sale.order"]
            recs = m.search(["&", ("state", "!=", "done"), ("state", "!=", "cancel")])
            fields = ["id", "name"]
            for i in recs.read(fields):
                sale_order_dict[i["name"]] = i["id"]

        # Preprocessing step 3
        # required for incremental export of MOs
        loc_id = {}
        if self.mode == 2:
            m = self.env["stock.location"]
            recs = m.search([])
            fields = ["id", "complete_name"]
            for i in recs.read(fields):
                loc_id[i["complete_name"]] = i["id"]

        # Read the xml file received from frepple
        # and store all that data into a list of tuples
        # (so_id, supplier_id, item_id, receipt date, quantity)
        pdp_temp = []
        missing_pos = set()

        for event, elem in iterparse(self.datafile, events=("start", "end")):
            if event == "end" and elem.tag == "operationplan":
                uom_id, item_id = elem.get("item_id").split(",")
                try:
                    ordertype = elem.get("ordertype")
                    if ordertype == "PO":
                        # find odoo po number for that supplier/batch
                        supplier_id = int(elem.get("supplier").split(" ", 1)[0])
                        uom_id, item_id = elem.get("item_id").split(",")
                        batch = elem.get("batch")

                        if batch not in sale_order_dict:
                            logger.info(
                                "couldn't find sales order %s in odoo database" % batch
                            )
                            continue

                        # convert end date to UTC (all dates are in UTC in odoo)
                        enddate = datetime.strptime(
                            elem.get("end")[:19], "%Y-%m-%d %H:%M:%S"
                        )
                        enddate = (
                            timezone("Asia/Hong_Kong")
                            .localize(enddate)
                            .astimezone(timezone("UTC"))
                        ).strftime("%Y-%m-%d %H:%M:%S")

                        pdp_temp.append(
                            (
                                batch,
                                sale_order_dict[batch],
                                supplier_id,
                                item_id,
                                enddate,
                                elem.get("quantity"),
                            )
                        )

                        if (batch, supplier_id) not in po_info_dict:
                            missing_pos.add(
                                (
                                    supplier_id,
                                    self.company.id,
                                    supplier_id,
                                    batch,
                                    supplier_id,
                                    supplier_id,
                                )
                            )
                        countproc += 1
                        if countproc % 100 == 0:
                            logger.info(
                                "processed so far %s POs received from frePPLe"
                                % countproc
                            )

                    elif ordertype == "MO":
                        logger.info(elem.get("start")[:19])
                        startdate = datetime.strptime(
                            elem.get("start")[:19], "%Y-%m-%d %H:%M:%S"
                        )
                        startdate = (
                            timezone("Asia/Hong_Kong")
                            .localize(startdate)
                            .astimezone(timezone("UTC"))
                        ).strftime("%Y-%m-%d %H:%M:%S")

                        enddate = datetime.strptime(
                            elem.get("end")[:19], "%Y-%m-%d %H:%M:%S"
                        )
                        enddate = (
                            timezone("Asia/Hong_Kong")
                            .localize(enddate)
                            .astimezone(timezone("UTC"))
                        ).strftime("%Y-%m-%d %H:%M:%S")

                        # Create manufacturing order
                        mo = mfg_order.create(
                            {
                                "product_qty": elem.get("quantity"),
                                "date_planned_start": startdate,
                                "date_planned_finished": enddate,
                                "product_id": int(item_id),
                                "company_id": self.company.id,
                                "product_uom_id": int(uom_id),
                                "location_src_id": loc_id["MH/WIP Stock"],
                                "location_dest_id": (
                                    loc_id["MH/Semi-Finished Goods"]
                                    if elem.get("ordered_item") == "false"
                                    else loc_id["MH/Finished Goods"]
                                ),
                                "bom_id": int(elem.get("operation").split(" ")[-1]),
                                "mh_tracking_no": elem.get("batch"),
                                "origin": "frePPLe",
                                "qty_producing": 0.00,
                            }
                        )
                        mo._onchange_workorder_ids()
                        mo._onchange_move_raw()
                        mo._create_update_move_finished()
                        countmfg += 1
                except Exception as e:
                    logger.error("Exception %s" % e)
                    msg.append(str(e))
                # Remove the element now to keep the DOM tree small
                root.clear()
            elif event == "start" and elem.tag == "operationplans":
                # Remember the root element
                root = elem

        if self.mode == 1:
            try:
                # we need to write all missing POs into odoo database
                sql = """
                insert into purchase_order
                (name, origin, partner_id, state, date_planned, date_order, currency_id, picking_type_id,
                invoice_count, invoice_status, amount_untaxed, amount_tax, amount_total,
                user_id, company_id, create_date, write_date, picking_count, x_studio_vendor_language,
                x_studio_vendor_id, currency_rate, date_calendar_start,
                mail_reminder_confirmed, mail_reception_confirmed, priority,
                mh_tracking_no, create_uid, write_uid,payment_term_id,fiscal_position_id)
                select 'PO'||to_char(now(),'YY')||'-'||nextval('purchase_order_id_seq'),
                'frePPLe',
                %s, --partner_id
                'draft',
                now(), --requested_date
                now(),
                7,
                21,
                0,
                'no',
                0,
                0,
                0,
                94,
                %s, --company_id
                now(),
                now(),
                0,
                'zh_CN',
                %s, -- partner_id
                1,
                now(),
                false,
                false,
                0,
                %s, -- sales order
                94,
                94,
                (select (min(split_part(value_reference,',',2)))::int from ir_property where name = 'property_supplier_payment_term_id'
                and res_id = 'res.partner,'||%s), --payment_term_id
                (select (min(split_part(value_reference,',',2)))::int from ir_property where name = 'property_account_position_id'
                and res_id = 'res.partner,'||%s) --fiscal_position_id
                """

                if len(missing_pos) > 0:
                    logger.info("Starting to create missing purchase orders")
                    self.env.cr.executemany(sql, [i for i in missing_pos])
                    logger.info("Finished creating missing purchase order records")

                # I need to read again the po ids now that they are all here
                m = self.env["purchase.order"]
                recs = m.search([("state", "=", "draft")])
                fields = ["id", "mh_tracking_no", "partner_id"]
                po_info_dict = {}
                for i in recs.read(fields):
                    po_info_dict[(i["mh_tracking_no"], i["partner_id"][0])] = i["id"]

                pdp_data = []
                for i in pdp_temp:
                    po_id = (
                        po_info_dict[(i[0], i[2])]
                        if (i[0], i[2]) in po_info_dict
                        else None
                    )
                    if po_id:
                        pdp_data.append((i[1], po_id, i[3], i[4], i[5]))

                # writing all delivery plan into odoo
                sql = """
                insert into pdp (so_id, po_id, product_id, requested_date, qty_to_receive, received_qty, status_id, create_uid, create_date, write_uid, write_date)
                values
                (%s,%s,%s,%s,%s,0,1,94, now(), 94, now())
                """
                if len(pdp_data) > 0:
                    logger.info("Starting to write pdp records")
                    self.env.cr.executemany(sql, pdp_data)
                    logger.info("Finished to write pdp records")

                # updating purchase order lines quantities
                logger.info(
                    "Updating purchase order lines quantity for existing records"
                )
                self.env.cr.execute(
                    """
                with cte as (
                    select po_id, product_id, sum(qty_to_receive) quantity, min(pdp.requested_date) requested_date
                    from pdp
                    inner join purchase_order on purchase_order.id = pdp.po_id and purchase_order.state = 'draft'
                    where pdp.requested_date is not null
                    group by po_id, product_id
                )
                update purchase_order_line
                set product_qty = cte.quantity,
                product_uom_qty = cte.quantity,
                x_qty_to_recv = cte.quantity,
                date_planned = cte.requested_date,
                write_date = now()
                from cte
                where cte.po_id = purchase_order_line.order_id
                and purchase_order_line.product_id = cte.product_id
                and (select count(*) from purchase_order_line pol
                    where pol.order_id = purchase_order_line.order_id
                    and pol.product_id = purchase_order_line.product_id) = 1
                """
                )
                logger.info("Finished to write missing purchase order lines")

                logger.info("Deleting PO lines that have nothing to do here")

                self.env.cr.execute(
                    """
                with cte as (
                select purchase_order_line.order_id, purchase_order_line.product_id from purchase_order_line
                inner join purchase_order on purchase_order.id = purchase_order_line.order_id
                and purchase_order.state = 'draft'
                and purchase_order.origin = 'frePPLe'
                except
                select po_id, product_id from pdp
                )
                delete from purchase_order_line
                using cte
                where cte.order_id = purchase_order_line.order_id
                and cte.product_id = purchase_order_line.product_id
                """
                )

                logger.info("Finished deleting PO lines that have nothing to do here")

                # writing missing purchase order lines
                logger.info("Starting to write missing purchase order lines")
                self.env.cr.execute(
                    """
                insert into purchase_order_line
                (name, sequence, product_qty, price_unit, product_uom_qty, date_planned, product_uom, product_id, order_id, company_id, state, partner_id, currency_id,
                create_uid, create_date, write_uid, write_date, mh_tracking_no, x_qty_to_recv, x_user_id, qty_received_method, propagate_cancel, qty_to_invoice)
                select product_template.name, 20, sum(pdp.qty_to_receive), product_template.list_price, sum(pdp.qty_to_receive), min(pdp.requested_date), product_template.uom_id, pdp.product_id,
                pdp.po_id, %s, 'draft', purchase_order.partner_id, 7, 94, now(), 1, now(), purchase_order.mh_tracking_no, sum(pdp.qty_to_receive), 94,
                'stock_moves', true, 0
                from pdp
                inner join product_product on product_product.id = pdp.product_id
                inner join product_template on product_template.id = product_product.product_tmpl_id
                inner join purchase_order on purchase_order.id = pdp.po_id and purchase_order.state = 'draft'
                where not exists (select 1 from purchase_order_line where order_id = pdp.po_id and product_id = pdp.product_id)
                group by
                product_template.name, product_template.list_price, product_template.uom_id, pdp.product_id,
                pdp.po_id, purchase_order.partner_id, purchase_order.mh_tracking_no
                """,
                    [
                        self.company.id,
                    ],
                )
                logger.info("Finished to write missing purchase order lines")

                # updating the price at PO line and PO levels
                self.env.cr.execute(
                    """
                with cte as (
                select row_number() over (partition by product_tmpl_id, partner_id order by min_qty, price) as id, product_tmpl_id, partner_id, min_qty, price  from
                (select product_tmpl_id, name as partner_id, min_qty, min(price) as price from product_supplierinfo
                group by product_tmpl_id, name, min_qty) t
                )
                update purchase_order_line
                set price_unit = cte.price, price_subtotal = cte.price*product_qty, price_total=cte.price*product_qty, price_tax = 0
                from cte, product_product
                where state = 'draft'
                and product_product.id = purchase_order_line.product_id
                and cte.partner_id = purchase_order_line.partner_id
                and cte.product_tmpl_id = product_product.product_tmpl_id
                and cte.min_qty <= purchase_order_line.product_qty
                and not exists (select 1 from cte cte2 where cte2.partner_id = purchase_order_line.partner_id
                and cte2.product_tmpl_id = product_product.product_tmpl_id
                and cte2.min_qty <= purchase_order_line.product_qty and cte2.min_qty < cte.min_qty)
                and (purchase_order_line.price_unit is distinct from cte.price
                    or price_subtotal is distinct from cte.price*product_qty
                    or price_total is distinct from cte.price*product_qty);
                with cte as (
                select purchase_order_line.order_id,
                sum(purchase_order_line.price_subtotal) price_subtotal,
                sum(purchase_order_line.price_total) price_total
                    from purchase_order_line
                    inner join purchase_order on purchase_order.id = purchase_order_line.order_id
                    and purchase_order.state = 'draft'
                group by order_id
                )
                update purchase_order
                set amount_untaxed = cte.price_subtotal,
                amount_tax = cte.price_total-cte.price_subtotal,
                amount_total = cte.price_total
                from cte
                where origin = 'frePPLe'
                and cte.order_id = purchase_order.id
                and (amount_untaxed is distinct from cte.price_subtotal
                or amount_tax is distinct from cte.price_total-cte.price_subtotal
                or amount_total is distinct from cte.price_total);
                """
                )
            except Exception as e:
                logger.error("Exception %s" % e)
                msg.append(str(e))

        # Be polite, and reply to the post
        msg.append("Processed %s uploaded procurement orders" % countproc)
        return "\n".join(msg)
