#!/usr/bin/env python
#
#
# Copyright (c) 2016 "Jonathan Yantis"
#
# This file is a part of NetGrph.
#
#    This program is free software: you can redistribute it and/or  modify
#    it under the terms of the GNU Affero General Public License, version 3,
#    as published by the Free Software Foundation.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#    As a special exception, the copyright holders give permission to link the
#    code of portions of this program with the OpenSSL library under certain
#    conditions as described in each individual source file and distribute
#    linked combinations including the program with the OpenSSL library. You
#    must comply with the GNU Affero General Public License in all respects
#    for all of the code used other than as permitted herein. If you modify
#    file(s) with this exception, you may extend this exception to your
#    version of the file(s), but you are not obligated to do so. If you do not
#    wish to do so, delete this exception statement from your version. If you
#    delete this exception statement from all source files in the program,
#    then also delete it in the license file.
#
#
"""NetGrph VLAN Import and Topology routines"""

import logging
import nglib

verbose = 0
logger = logging.getLogger(__name__)


def import_vlans(fileName, ignore_new=False):
    """Import a list of VLANs for every switch"""

    logger.info("Importing List of VLANs from " + fileName)

    vdb = nglib.importCSVasDict(fileName)

    # Import all VLAN nodes and link to MGMT Group
    import_mgmt_vlan(vdb, ignore_new)

    logger.info("Linking VLANs to Switches")

    vdb = nglib.importCSVasDict(fileName)

    # Link VLAN to Switch
    for en in vdb:
        link_vlan_switch(en)


def link_vlan_switch(en):
    """Link a VLAN to a Switch (Bolt Driver)"""

    time = nglib.get_time()

    vname = en['MGMT'] + "-" + en['VID']
    desc = en['VName']
    switch = en['Switch']
    stp = en['STP']

    results = nglib.bolt_ses.run(
        'MATCH (n:VLAN {name:{vname}})-[e:Switched]->(s:Switch {name:{switch}}) '
        + 'RETURN n.name AS name',
        {"vname": vname, "switch": switch})

    # Check for Results FIXME
    try:
        name = next(iter(results))
        logger.debug("Updating: VLAN (%s)-[:Switched]->(%s) Relationship", vname, switch)

    except:
        logger.info("New: VLAN (%s)-[:Switched]->(%s) Relationship", vname, switch)

    nglib.bolt_ses.run(
        'MATCH (v:VLAN {name:{vname}}), (s:Switch {name:{switch}}) ' +
        'MERGE (v)-[e:Switched]->(s) SET e += {desc:{desc}, stp:{stp}, time:{time}} RETURN e',
        {"vname": vname, "switch": switch, "desc": desc, "stp": stp, "time": time})


def import_mgmt_vlan(vdb, ignore_new):
    """Collate all MGMT-VID pairs, insert nodes and link to MgmtGroup"""


    vuniq = dict()

    time = nglib.get_time()

    for en in vdb:
        vname = en['MGMT'] + "-" + en['VID']
        vuniq[vname] = 1

    for en in vuniq.keys():
        vname = en
        (mgmt, vid) = vname.split('-')
        vid = str(vid)

        results = nglib.py2neo_ses.cypher.execute(
            'MATCH (n:VLAN {name:{vname}}) RETURN n',
            vname=vname)

        # Add new VLAN
        if len(results) == 0:
            logger.info("New: Inserting VLAN %s", en)

            results = nglib.py2neo_ses.cypher.execute(
                'CREATE (v:VLAN {name:{vname}, vid:{vid}, mgmt:{mgmt}, time:{time}}) RETURN v',
                vname=vname, vid=vid, mgmt=mgmt, time=time)

            # Record New Network Unless Ignoring initial run
            if not ignore_new:
                # Store a NewVLAN Object for alerting
                nglib.py2neo_ses.cypher.execute(
                    'CREATE (v:NewVLAN {name:{vname}, time:{time}}) RETURN v',
                    vname=vname, time=time)

        # Else update record
        else:
            logger.debug("Updating VLAN %s", vname)
            nglib.py2neo_ses.cypher.execute(
                'MATCH (v:VLAN {name:{vname}}) SET v += '
                + '{vid:{vid}, mgmt:{mgmt}, time:{time}} RETURN v',
                vname=vname, vid=vid, mgmt=mgmt, time=time)


def update_vlans():
    """Run VLAN update routines"""

    logger.info("Updating VLAN Topology")

    # Update descriptions
    update_vlan_desc()

    # Update Bridge Domains
    update_bidge_domains()

    # Root election
    root_election()


def root_election():
    """Kick off a root election for VLANs"""

    # Find the local root for each switch domain
    find_local_root()

    # Search all bridge trees for lowest STP and link the root domain to the root
    find_bridged_root()


def update_vlan_desc():
    """Update VLAN descriptions using election process on each switch in domain"""

    results = nglib.py2neo_ses.cypher.execute(
        'MATCH (v:VLAN) RETURN v.name as vname')

    # Get all VLANs
    if len(results) > 0:
        for v in results:
            vname = v.vname
            descdb = dict()

            # Get vlan desc properties for each switch from relationship
            results = nglib.py2neo_ses.cypher.execute(
                'MATCH (v:VLAN {name:{vname}})-[e:Switched]-() RETURN e.desc AS desc',
                vname=vname)

            # Get all descriptions from relationships to switches and count them
            if len(results) > 0:
                for d in results:
                    desc = d.desc
                    if desc != "NONAME":
                        if desc not in descdb.keys():
                            descdb[desc] = 1
                        else:
                            descdb[desc] = descdb[desc] + 1

            # Top Value Found
            if descdb.keys():
                topDesc = max(descdb.keys())

            logger.debug("Updating top description for VLAN:%s Desc:%s", vname, topDesc)

            nglib.py2neo_ses.cypher.execute(
                'MATCH (v:VLAN {name:{vname}}) SET v.desc={topDesc} RETURN v',
                vname=vname, topDesc=topDesc)


def update_bidge_domains():
    """Update all vlan bridges between vlan management domains"""

    # Get all Switches and their child neighbors
    results = nglib.py2neo_ses.cypher.execute(
        'MATCH (ps:Switch)-[e:NEI|NEI_EQ]->(cs:Switch) '
        + 'RETURN ps.name as pswitch, ps.mgmt AS pmgmt, cs.name as cswitch, cs.mgmt AS cmgmt')

    if len(results) > 0:
        for r in results.records:

            # Different MGMT Domain and adjacent, look to bridge VLANs
            if r.pmgmt != r.cmgmt:

                # Get all VIDs for both parent and child switches
                pvlans = nglib.py2neo_ses.cypher.execute(
                    'MATCH (ps:Switch {name:{pswitch}})<-[e:Switched]-(v:VLAN) '
                    + 'RETURN v.vid as vid',
                    pswitch=r.pswitch)
                cvlans = nglib.py2neo_ses.cypher.execute(
                    'MATCH (ps:Switch {name:{cswitch}})<-[e:Switched]-(v:VLAN) '
                    + 'RETURN v.vid as vid',
                    cswitch=r.cswitch)

                # Bridge VLANs across MGMT Domains
                if len(pvlans) > 0 and len(cvlans) > 0:
                    pvdb = dict()
                    cvdb = dict()

                    # Load dicts of vlan IDs both both parent and child
                    for p in pvlans.records:
                        pvdb[p.vid] = 1
                    for c in cvlans.records:
                        cvdb[c.vid] = 1

                    # If VIDs Match between parent and child across mgmt domains,
                    # bridge the two
                    for vlan in pvdb.keys():
                        if vlan in cvdb.keys():
                            update_bridge(r.pmgmt, r.cmgmt, vlan, r.pswitch, r.cswitch)


def update_bridge(pmgmt, cmgmt, vlan, pswitch, cswitch):
    """Insert or Update a VLAN BRIDGE"""

    if verbose > 2:
        print("Bridge: ", pmgmt, cmgmt, vlan, pswitch, cswitch)

    pvlan = pmgmt + "-" + vlan
    cvlan = cmgmt + "-" + vlan
    time = nglib.get_time()

    # See if a Bridge Exists
    results = nglib.py2neo_ses.cypher.execute(
        'MATCH (pv:VLAN {name:{pvlan}})-[e:BRIDGE]-(cv:VLAN {name:{cvlan}}) RETURN e',
        pvlan=pvlan, cvlan=cvlan)

    if len(results) == 0:
        logger.info("New: Bridge (%s)-[:BRIDGE]->(%s) Relationship", pvlan, cvlan)

        nglib.py2neo_ses.cypher.execute(
            'MATCH (pv:VLAN {name:{pvlan}}), (cv:VLAN {name:{cvlan}}) '
            + 'CREATE (pv)-[e:BRIDGE {pswitch:{pswitch}, cswitch:{cswitch}, time:{time}}]'
            + '->(cv) RETURN e',
            pvlan=pvlan, cvlan=cvlan, pswitch=pswitch, cswitch=cswitch, time=time)

    else:
        logger.debug("Updating VLAN %s-[:BRIDGE]->%s Relationship", pvlan, cvlan)

        results = nglib.py2neo_ses.cypher.execute(
            'MATCH (pv:VLAN {name:{pvlan}})-[e:BRIDGE]->(cv:VLAN {name:{cvlan}}) '
            + 'SET e += {pswitch:{pswitch}, cswitch:{cswitch}, time:{time}} RETURN e',
            pvlan=pvlan, cvlan=cvlan, pswitch=pswitch, cswitch=cswitch, time=time)



def find_local_root():
    """
    Go through every Switch in a management domain
    Find the lowest STP value and assume root within domain
    """

    results = nglib.py2neo_ses.cypher.execute(
        'MATCH (v:VLAN)-[:Switched]->() RETURN DISTINCT(v.name) AS name, v.vid AS vid')

    # Find the local root for vid on each switch
    if len(results) > 0:
        for v in results.records:
            vname = v.name
            stpmin = 32768
            switch = None

            # Get STP values from all Switched Relationships
            results = nglib.py2neo_ses.cypher.execute(
                'MATCH (v:VLAN {name:{vname}})-[e:Switched]->(s) '
                + 'RETURN e.stp AS stp, s.name AS switch ORDER BY switch',
                vname=vname)

            # Find the lowest value
            for s in results.records:
                stp = int(s.stp)
                if stp < stpmin and stp != 0:
                    stpmin = stp
                    switch = s.switch
                    if verbose > 3:
                        print("Local Root: ", vname, stp, switch)

            # Update VLAN with lowest value
            results = nglib.py2neo_ses.cypher.execute(
                'MATCH (v:VLAN {name:{vname}}) SET v += {lroot:{switch}, lstp:{stp}}',
                vname=vname, switch=switch, stp=stpmin)


def find_bridged_root():
    """Go through each VLAN, search all BRIDGED nodes for lowest STP value"""

    # Get all VLANs
    results = nglib.py2neo_ses.cypher.execute(
        'MATCH (v:VLAN) RETURN v.name AS name')

    if len(results) > 0:
        for r in results.records:
            vname = r.name
            stp = 32768
            rootSwitch = None

            # Find Bridged VLANs first
            bridged = nglib.py2neo_ses.cypher.execute(
                'MATCH (v:VLAN {name:{vname}})-[e:BRIDGE*]-(b:VLAN) '
                + 'RETURN b.name AS name, b.lstp AS lstp, b.lroot AS lroot',
                vname=vname)

            # Local Values
            local = nglib.py2neo_ses.cypher.execute(
                'MATCH (v:VLAN {name:{vname}}) '
                + 'RETURN v.name AS name, v.lstp AS lstp, v.lroot AS lroot',
                vname=vname)

            if len(bridged) > 0:
                for b in bridged:

                    # New Lowest STP Domain
                    if int(b.lstp) < stp:
                        #print("Low STP: ",vname,b.name,b.lstp,b.lroot)
                        stp = int(b.lstp)

            # Check local stp values
            if len(local) > 0:
                v = local.records[0]

                # If local root is the root for the BRIDGE domain, create root relationship
                if int(v.lstp) <= stp:
                    stp = int(v.lstp)
                    rootSwitch = v.lroot

                    # Link Bridge domain to root
                    if stp < 32768:
                        if verbose > 3:
                            print("Low STP: ", vname, stp, rootSwitch)
                        link_vlan_to_root(vname, stp, rootSwitch)


def link_vlan_to_root(vname, stp, rootSwitch):
    """Create a VLAN -[ROOT]-> Switch Relationship"""

    root = nglib.py2neo_ses.cypher.execute(
        'MATCH (v:VLAN {name:{vname}})-[e:ROOT]-(s:Switch {name:{rootSwitch}}) RETURN e',
        vname=vname, rootSwitch=rootSwitch)

    time = nglib.get_time()

    # Create New Root Relationship
    if len(root) == 0:
        logger.info("New: Root for VLAN (%s)-[:ROOT]->(%s)", vname, rootSwitch)

        nglib.py2neo_ses.cypher.execute(
            'MATCH (v:VLAN {name:{vname}}),(s:Switch {name:{rootSwitch}}) '
            + 'CREATE (v)-[e:ROOT {stp:{stp}, time:{time}}]->(s) RETURN e',
            vname=vname, rootSwitch=rootSwitch, stp=stp, time=time)

    # Update existing
    else:
        logger.debug("Updating Root for VLAN (%s)-[:ROOT]->(%s)", vname, rootSwitch)

        nglib.py2neo_ses.cypher.execute(
            'MATCH (v:VLAN {name:{vname}})-[e:ROOT]->(s:Switch {name:{rootSwitch}}) '
            + 'SET e += {stp:{stp}, time:{time}} RETURN e',
            vname=vname, rootSwitch=rootSwitch, stp=stp, time=time)

def netdb_vlan_import():
    """For all (switch, vlan) entries, get mac and port counts"""

    logger.info("Update: Importing MAC and Ports Counts on VLANs from NetDB")

    switchvlans = nglib.bolt_ses.run(
        'MATCH (v:VLAN)-[e:Switched]->(s:Switch) '
        + 'RETURN s.name AS switch, v.vid AS vid, v.name AS vname')

    for en in switchvlans:

        (pcount, mcount) = nglib.netdb.get_mac_and_port_counts(en['switch'], en['vid'])

        updatevlans = nglib.bolt_ses.run(
            'MATCH (v:VLAN {name:{vname}})-[e:Switched]->(s:Switch {name:{switch}}) '
            + 'SET e += {pcount:{pcount}, mcount:{mcount}} RETURN e',
            {"vname": en['vname'], "switch": en['switch'], "pcount": pcount, "mcount": mcount})


# END