# -*- coding: utf-8 -*-
##############################################################################
#    
#    OpenERP, Open Source Management Solution
#    Copyright (C) 2004-2009 Tiny SPRL (<http://tiny.be>).
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as
#    published by the Free Software Foundation, either version 3 of the
#    License, or (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.     
#
##############################################################################
"""
Module to handle publisher warranty contracts as well as notifications from
OpenERP.
"""

from osv import osv, fields
import logging
from tools.translate import _
import urllib
import urllib2
from tools.safe_eval import safe_eval
import pooler
from tools.config import config
import release
import datetime
from tools import misc
import sys

_logger = logging.getLogger(__name__)

"""
Time interval that will be used to determine up to which date we will
check the logs to see if a message we just received was already logged.
@type: datetime.timedelta
"""
_PREVIOUS_LOG_CHECK = datetime.timedelta(days=365)

class publisher_warranty_contract(osv.osv):
    """
    Osv representing a publisher warranty contract.
    """
    _name = "publisher_warranty.contract"

    def _get_valid_contracts(self, cr, uid):
        """
        Return the list of the valid contracts encoded in the system.
        
        @return: A list of contracts
        @rtype: list of publisher_warranty.contract browse records
        """
        return [contract for contract in self.browse(cr, uid, self.search(cr, uid, []))
                if contract.state == 'valid']
    
    def status(self, cr, uid):
        """ Method called by the client to check availability of publisher warranty contract. """
        
        contracts = self._get_valid_contracts(cr, uid)
        return {
            'status': "full" if contracts else "none" ,
            'uncovered_modules': list(),
        }
    
    def send(self, cr, uid, tb, explanations, remarks=None):
        """ Method called by the client to send a problem to the publisher warranty server. """
        
        if not remarks:
            remarks = ""

        valid_contracts = self._get_valid_contracts(cr, uid)
        valid_contract = valid_contracts[0]
        
        try:
            origin = 'client'
            dbuuid = self.pool.get('ir.config_parameter').get_param(cr, uid, 'database.uuid')
            db_create_date = self.pool.get('ir.config_parameter').get_param(cr, uid, 'database.create_date')
            
            msg = {'contract_name': valid_contract.name,
                'tb': tb,
                'explanations': explanations,
                'remarks': remarks,
                'origin': origin,
                'dbname': cr.dbname,
                'dbuuid': dbuuid,
                'db_create_date': db_create_date}
            
            
            add_arg = {"timeout":30} if sys.version_info >= (2,6) else {}
            uo = urllib2.urlopen(config.get("publisher_warranty_url"),
                                    urllib.urlencode({'arg0': msg, "action": "send",}),**add_arg)
            try:
                submit_result = uo.read()
            finally:
                uo.close()
            
            result = safe_eval(submit_result)
            
            crm_case_id = result
            
            if not crm_case_id:
                return False
            
        except osv.except_osv:
            raise
        except:
            _logger.warning("Error sending problem report", exc_info=1)
            raise osv.except_osv("Connection error", "An error occured during the connection " +
                                 "with the publisher warranty server.")
        
        return True
    
    def check_validity(self, cr, uid, ids, context={}):
        """
        Check the validity of a publisher warranty contract. This method just call get_logs() but checks
        some more things, so it can be called from a user interface.
        """
        contract_id = ids[0]
        contract = self.browse(cr, uid, contract_id)
        state = contract.state
        validated = state != "unvalidated"
        
        self.get_logs(cr, uid, ids, cron_mode=False, context=context)
        
        contract = self.browse(cr, uid, contract_id)
        validated2 = contract.state != "unvalidated"
        if not validated and not validated2:
            raise osv.except_osv(_("Contract validation error"),
                                 _("Please check your publisher warranty contract name and validity."))
    
    def get_logs(self, cr, uid, ids, cron_mode=True, context={}):
        """
        Send a message to OpenERP's publisher warranty server to check the validity of
        the contracts, get notifications, etc...
        
        @param cron_mode: If true, catch all exceptions (appropriate for usage in a cron).
        @type cron_mode: boolean
        """
        try:
            try:
                result = get_sys_logs(cr, uid)
            except:
                _logger.debug("Exception while sending a get logs messages", exc_info=1)
                raise osv.except_osv(_("Error"), _("Error during communication with the publisher warranty server."))
            
            contracts = result["contracts"]
            for contract in contracts:
                c_id = self.search(cr, uid, [("name","=",contract)])[0]
                date_from = contracts[contract][0]
                date_to = contracts[contract][1]
                state = contracts[contract][2]
                self.write(cr, uid, c_id, {
                    "date_start": date_from,
                    "date_stop": date_to,
                    "state": state,
                })
            
            limit_date = (datetime.datetime.now() - _PREVIOUS_LOG_CHECK).strftime(misc.DEFAULT_SERVER_DATETIME_FORMAT)
            for message in result["messages"]:
                ids = self.pool.get("res.log").search(cr, uid, [("res_model", "=", "publisher_warranty.contract"),
                                                          ("create_date", ">=", limit_date),
                                                          ("name", "=", message)])
                if ids:
                    continue
                self.pool.get('res.log').create(cr, uid,
                        {
                            'name': message,
                            'res_model': "publisher_warranty.contract",
                            "read": True,
                            "user_id": False,
                        },
                        context=context
                )
        except:
            _logger.debug("Exception while interpreting the result of a logs message", exc_info=1)
            if cron_mode:
                return False # same as before
            else:
                raise
        return True
    
    def get_last_user_messages(self, cr, uid, limit, context={}):
        """
        Get the messages to be written in the web client.
        @return: A list of html messages with ids, can be False or empty.
        @rtype: list of tuples(int,string)
        """
        ids = self.pool.get('res.log').search(cr, uid, [("res_model", "=", "publisher_warranty.contract")]
                                        , order="create_date desc", limit=limit)
        if not ids:
            return []
        messages = [(x.id, x.name) for x in self.pool.get('res.log').browse(cr, uid, ids)]
    
        return messages
    
    def del_user_message(self, cr, uid, id, context={}):
        """
        Delete a message.
        """
        self.pool.get('res.log').unlink(cr, uid, [id])
        
        return True

    _columns = {
        'name' : fields.char('Serial Key', size=384, required=True),
        'date_start' : fields.date('Starting Date', readonly=True),
        'date_stop' : fields.date('Ending Date', readonly=True),
        'state' : fields.selection([('unvalidated', 'Unvalidated'), ('valid', 'Valid')
                            , ('terminated', 'Terminated'), ('canceled', 'Canceled')], string="State", readonly=True),
        'kind' : fields.char('Kind', size=64, readonly=True),
    }
    
    _defaults = {
        'state': 'unvalidated',
    }
    
    _sql_constraints = [
        ('uniq_name', 'unique(name)', "Your publisher warranty contract is already subscribed in the system !")
    ]

publisher_warranty_contract()

class maintenance_contract(osv.osv_memory):
    """ Old osv we only keep for compatibility with the clients. """
    
    _name = "maintenance.contract"
    
    def status(self, cr, uid):
        return self.pool.get("publisher_warranty.contract").status(cr, uid)
        
    def send(self, cr, uid, tb, explanations, remarks=None):
        return self.pool.get("publisher_warranty.contract").send(cr, uid, tb, explanations, remarks)
    
maintenance_contract()

class publisher_warranty_contract_wizard(osv.osv_memory):
    """
    A wizard osv to help people entering a publisher warranty contract.
    """
    _name = 'publisher_warranty.contract.wizard'
    _inherit = "ir.wizard.screen"

    _columns = {
        'name' : fields.char('Serial Key', size=256, required=True ),
        'state' : fields.selection([("draft", "Draft"), ("finished", "Finished")])
    }
    
    _defaults = {
        "state": "draft",
    }

    def action_validate(self, cr, uid, ids, context=None):
        if not ids:
            return False

        wiz = self.browse(cr, uid, ids[0])
        c_name = wiz.name
        
        contract_osv = self.pool.get("publisher_warranty.contract")
        contracts = contract_osv.search(cr, uid, [("name","=",c_name)])
        if contracts:
            raise osv.except_osv(_("Error"), _("That contract is already registered in the system."))
        
        contract_id = contract_osv.create(cr, uid, {
            "name": c_name,
            "state": "unvalidated",
        })
        
        contract_osv.check_validity(cr, uid, [contract_id])
        
        self.write(cr, uid, ids, {"state": "finished"})
        
        return True


publisher_warranty_contract_wizard()

def get_sys_logs(cr, uid):
    """
    Utility method to send a publisher warranty get logs messages.
    """
    pool = pooler.get_pool(cr.dbname)
    
    dbuuid = pool.get('ir.config_parameter').get_param(cr, uid, 'database.uuid')
    db_create_date = pool.get('ir.config_parameter').get_param(cr, uid, 'database.create_date')
    nbr_users = pool.get("res.users").search(cr, uid, [], count=True)
    contractosv = pool.get('publisher_warranty.contract')
    contracts = contractosv.browse(cr, uid, contractosv.search(cr, uid, []))
    user = pool.get("res.users").browse(cr, uid, uid)
    msg = {
        "dbuuid": dbuuid,
        "nbr_users": nbr_users,
        "dbname": cr.dbname,
        "db_create_date": db_create_date,
        "version": release.version,
        "contracts": [c.name for c in contracts],
        "language": user.context_lang,
    }
    
    add_arg = {"timeout":30} if sys.version_info >= (2,6) else {}
    uo = urllib2.urlopen(config.get("publisher_warranty_url"),
                        urllib.urlencode({'arg0': msg, "action": "update",}), **add_arg)
    try:
        submit_result = uo.read()
    finally:
        uo.close()
    
    result = safe_eval(submit_result)
    
    return result

# vim:expandtab:smartindent:tabstop=4:softtabstop=4:shiftwidth=4:

