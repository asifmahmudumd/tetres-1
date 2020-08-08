# -*- coding: utf-8 -*-
import json
import statistics
from typing import List

import numpy as np
from colorama import Fore
from pyticas.moe import moe
from pyticas.moe.imputation import spatial_avg
from pyticas.moe.mods import total_flow_with_virtual_nodes, speed_with_virtual_nodes, density_with_virtual_nodes, \
    lanes_with_virtual_nodes
from pyticas.moe.mods.cm import calculate_cm_dynamically
from pyticas.moe.mods.cmh import calculate_cmh_dynamically
from pyticas.moe.mods.lvmt import calculate_lvmt_dynamically
from pyticas.moe.mods.uvmt import calculate_uvmt_dynamically
from pyticas.rc import route_config
from pyticas.tool import tb
from pyticas.ttypes import RNodeData
from pyticas_tetres.cfg import TT_DATA_INTERVAL
from pyticas_tetres.da.route import TTRouteDataAccess
from pyticas_tetres.da.route_wise_moe_parameters import RouteWiseMOEParametersDataAccess
from pyticas_tetres.da.tt import TravelTimeDataAccess
from pyticas_tetres.logger import getLogger
from pyticas_tetres.util.noop_context import nonop_with
from pyticas_tetres.util.systemconfig import get_system_config_info

"""
Travel Time Calculation
=======================

- run at beginning of the day to calculate tt of the previous day for all TTR routes

"""

__author__ = 'Chongmyung Park (chongmyung.park@gmail.com)'


def calculate_all_routes(prd, **kwargs):
    """ calculate travel time, average speed and VMT during the given time period
    and put whole_data to database (travel time table)

    :type prd: pyticas.ttypes.Period
    :rtype: list[dict]
    """
    logger = getLogger(__name__)
    logger.info('calculating travel time : %s' % prd.get_period_string())

    res = []
    ttr_route_da = TTRouteDataAccess()
    routes = ttr_route_da.list()
    ttr_route_da.close_session()
    total = len(routes)
    for ridx, ttri in enumerate(routes):
        logger.info('(%d/%d) calculating travel time for %s(%s) : %s'
                    % ((ridx + 1), total, ttri.name, ttri.id, prd.get_period_string()))
        is_inserted = calculate_a_route(prd, ttri, lock=kwargs.get('lock', nonop_with()))
        res.append({'route_id': ttri.id, 'done': is_inserted})

    return res


def print_rnode_data(lst: List[RNodeData]) -> None:
    for i in range(len(lst)):
        print(f"> {Fore.MAGENTA}Title[{lst[i].get_title(no_lane_info=True)}] DataListLength[{len(lst[i].data)}]")


def calculate_a_route(prd, ttri, **kwargs):
    """

    :type prd: pyticas.ttypes.Period
    :type ttri: pyticas_tetres.ttypes.TTRouteInfo
    """
    create_or_update = kwargs.get("create_or_update", False)
    logger = getLogger(__name__)
    dbsession = kwargs.get('dbsession', None)

    cur_config = get_system_config_info()

    if dbsession:
        da_tt = TravelTimeDataAccess(prd.start_date.year, session=dbsession)
    else:
        da_tt = TravelTimeDataAccess(prd.start_date.year)
    creatable_list = list()
    updatable_dict = {}
    existing_data_dict = {}
    if create_or_update:
        existing_data_list = da_tt.list_by_period(ttri.id, prd)
        for existing_data in existing_data_list:
            existing_data_dict[(existing_data.route_id, existing_data.time)] = existing_data
    print(f"{Fore.LIGHTBLUE_EX}"
          f" moe_crit_dens[{cur_config.moe_critical_density}]"
          f" moe_lane_cap[{cur_config.moe_lane_capacity}]"
          f" moe_cong_thresh_speed[{cur_config.moe_congestion_threshold_speed}]")
    lock = kwargs.get('lock', nonop_with())
    if not create_or_update:
        # delete data to avoid duplicated data
        with lock:
            is_deleted = da_tt.delete_range(ttri.id, prd.start_date, prd.end_date, print_exception=True)
            if not is_deleted or not da_tt.commit():
                logger.warning('fail to delete the existing travel time data')
                if not dbsession:
                    da_tt.close_session()
                return False

    print(f"{Fore.GREEN}CALCULATING TRAVEL-TIME FOR ROUTE[{ttri.name}]")
    latest_moe_parameter_object = None
    try:
        rw_moe_da = RouteWiseMOEParametersDataAccess()
        latest_moe_parameter_object = rw_moe_da.get_latest_moe_param_for_a_route(ttri.id)
        rw_moe_da.close_session()
    except Exception as e:
        logger = getLogger(__name__)
        logger.warning('fail to fetch the latest MOE parameter for this route. Error: {}'.format(e))
    if latest_moe_parameter_object:
        res_dict = _calculate_tt(ttri.route, prd, latest_moe_parameter_object.moe_critical_density,
                                 latest_moe_parameter_object.moe_lane_capacity,
                                 latest_moe_parameter_object.moe_congestion_threshold_speed)
    else:
        res_dict = _calculate_tt(ttri.route, prd, cur_config.moe_critical_density, cur_config.moe_lane_capacity,
                                 cur_config.moe_congestion_threshold_speed)

    if res_dict is None or res_dict['tt'] is None:
        logger.warning('fail to calculate travel time')
        return False

    travel_time = res_dict['tt'][-1].data
    avg_speeds = _route_avgs(res_dict['speed'])
    total_vmt = _route_total(res_dict['vmt'])  # flow
    res_vht = _route_total(res_dict['vht'])  # speed
    res_dvh = _route_total(res_dict['dvh'])  # flow
    res_lvmt = _route_total(res_dict['lvmt'])  # density
    res_uvmt = _route_total(res_dict['uvmt'])
    res_cm = _route_total(res_dict['cm'])
    res_cmh = _route_total(res_dict['cmh'])
    res_acceleration = _route_avgs(res_dict['acceleration'])
    raw_flow_data = res_dict["raw_flow_data"]
    raw_speed_data = res_dict["raw_speed_data"]
    raw_lane_data = res_dict['raw_lane_data']
    raw_density_data = res_dict['raw_density_data']
    raw_speed_data_without_virtual_node = res_dict['speed']
    res_mrf = res_dict["mrf"]
    timeline = prd.get_timeline(as_datetime=False, with_date=True)
    print(f"{Fore.CYAN}Start[{timeline[0]}] End[{timeline[-1]}] TimelineLength[{len(timeline)}]")
    for index, dateTimeStamp in enumerate(timeline):
        if latest_moe_parameter_object:
            meta_data = generate_meta_data(raw_flow_data, raw_speed_data, raw_lane_data, raw_density_data,
                                           raw_speed_data_without_virtual_node, res_mrf, latest_moe_parameter_object,
                                           index)
        else:
            meta_data = generate_meta_data(raw_flow_data, raw_speed_data, raw_lane_data, raw_density_data,
                                           raw_speed_data_without_virtual_node, res_mrf, cur_config, index)
        meta_data_string = json.dumps(meta_data)
        tt_data = {
            'route_id': ttri.id,
            'time': dateTimeStamp,
            'tt': travel_time[index],
            'speed': avg_speeds[index],
            'vmt': total_vmt[index],
            'vht': res_vht[index],
            'dvh': res_dvh[index],
            'lvmt': res_lvmt[index],
            'uvmt': res_uvmt[index],
            'cm': res_cm[index],
            'cmh': res_cmh[index],
            'acceleration': res_acceleration[index],
            'meta_data': meta_data_string,

        }
        if create_or_update:
            existing_data = existing_data_dict.get((tt_data['route_id'], tt_data['time']))
            if existing_data:
                updatable_dict[existing_data.id] = tt_data
            else:
                creatable_list.append(tt_data)
        else:
            creatable_list.append(tt_data)
    inserted_ids = list()
    if creatable_list:
        with lock:
            inserted_ids = da_tt.bulk_insert(creatable_list)
            if not inserted_ids or not da_tt.commit():
                logger.warning('fail to insert the calculated travel time into database')
    if not inserted_ids:
        inserted_ids = list()
    if updatable_dict:
        with lock:
            for id, tt_data in updatable_dict.items():
                da_tt.update(id, generate_updatable_moe_dict(tt_data))
                inserted_ids.append(id)
            da_tt.commit()
    if not dbsession:
        da_tt.close_session()
    return inserted_ids


def update_moe_values_a_route(prd, ttri_id, **kwargs):
    """

    :type prd: pyticas.ttypes.Period
    :type ttri: pyticas_tetres.ttypes.TTRouteInfo
    """
    import json
    dbsession = kwargs.get('dbsession', None)

    if dbsession:
        da_tt = TravelTimeDataAccess(prd.start_date.year, session=dbsession)
    else:
        da_tt = TravelTimeDataAccess(prd.start_date.year)
    rw_moe_param_json = kwargs.get("rw_moe_param_json")
    updatable_dict = {}
    existing_data_list = da_tt.list_by_period(ttri_id, prd)
    for existing_data in existing_data_list:
        meta_data = json.loads(existing_data.meta_data)
        updatable_data = dict()
        if meta_data.get('moe_congestion_threshold_speed') != rw_moe_param_json.get(
                'rw_moe_congestion_threshold_speed'):
            cm = (calculate_cm_dynamically(meta_data, rw_moe_param_json.get('rw_moe_congestion_threshold_speed')))
            cmh = (calculate_cmh_dynamically(meta_data, TT_DATA_INTERVAL,
                                             rw_moe_param_json.get('rw_moe_congestion_threshold_speed')))
            if existing_data.cm != cm:
                updatable_data['cm'] = cm
            if existing_data.cmh != cmh:
                updatable_data['cmh'] = cmh
            meta_data['moe_congestion_threshold_speed'] = rw_moe_param_json.get('rw_moe_congestion_threshold_speed')
            updatable_data['meta_data'] = json.dumps(meta_data)
        if meta_data.get('moe_lane_capacity') != rw_moe_param_json.get('rw_moe_lane_capacity') or meta_data.get(
                'moe_critical_density') != rw_moe_param_json.get('rw_moe_critical_density'):
            lvmt = calculate_lvmt_dynamically(meta_data, TT_DATA_INTERVAL,
                                              rw_moe_param_json.get('rw_moe_critical_density'),
                                              rw_moe_param_json.get('rw_moe_lane_capacity'))
            uvmt = calculate_uvmt_dynamically(meta_data, TT_DATA_INTERVAL,
                                              rw_moe_param_json.get('rw_moe_critical_density'),
                                              rw_moe_param_json.get('rw_moe_lane_capacity'))
            if existing_data.lvmt != lvmt:
                updatable_data['lvmt'] = lvmt
            if existing_data.uvmt != uvmt:
                updatable_data['uvmt'] = uvmt
            meta_data['moe_lane_capacity'] = rw_moe_param_json.get('rw_moe_lane_capacity')
            meta_data['moe_critical_density'] = rw_moe_param_json.get('rw_moe_critical_density')
            updatable_data['meta_data'] = json.dumps(meta_data)
        if updatable_data:
            updatable_dict[existing_data.id] = updatable_data
    lock = kwargs.get('lock', nonop_with())
    if updatable_dict:
        with lock:
            for id, updatable_data in updatable_dict.items():
                da_tt.update(id, updatable_data)
            da_tt.commit()
    da_tt.close_session()


def generate_updatable_moe_dict(tt_data):
    return {
        'vht': tt_data['vht'],
        'dvh': tt_data['dvh'],
        'lvmt': tt_data['lvmt'],
        'uvmt': tt_data['uvmt'],
        'cm': tt_data['cm'],
        'cmh': tt_data['cmh'],
        'acceleration': tt_data['acceleration'],
        'meta_data': tt_data['meta_data'],
    }


def generate_meta_data(raw_flow_data, raw_speed_data, raw_lane_data, raw_density_data,
                       raw_speed_data_without_virtual_node, mrf_data, cur_config, time_index):
    logger = getLogger(__name__)
    raw_meta_data = {
        "flow": [],
        "speed": [],
        "density": [],
        "lanes": [],
        "speed_average": 0,
        "speed_variance": 0,
        "speed_max_u": 0,
        "speed_min_u": 0,
        "speed_difference": 0,
        "number_of_vehicles_entered": 0,
        "number_of_vehicles_exited": 0,
        "moe_lane_capacity": cur_config.moe_lane_capacity,
        "moe_critical_density": cur_config.moe_critical_density,
        "moe_congestion_threshold_speed": cur_config.moe_congestion_threshold_speed}
    for flow, speed, lanes, density in zip(raw_flow_data, raw_speed_data, raw_lane_data, raw_density_data):
        raw_meta_data['flow'].append(flow[time_index])
        raw_meta_data['speed'].append(speed[time_index])
        raw_meta_data['density'].append(density[time_index])
        raw_meta_data['lanes'].append(lanes)
    speed_meta_data = list()
    for speed_rnode_data in raw_speed_data_without_virtual_node:
        if speed_rnode_data and speed_rnode_data.data:
            speed_data = speed_rnode_data.data
            if speed_data[time_index]:
                speed_meta_data.append(speed_data[time_index])
    try:
        ent_data = [rnd.data[time_index] for rnd in mrf_data
                    if rnd.data[time_index] > 0 and not isinstance(rnd.rnode, str) and rnd.rnode.is_entrance()]
        ent_total = sum(ent_data)
        raw_meta_data["number_of_vehicles_entered"] = ent_total
    except Exception as e:
        logger.warning('fail to calculate number of vehicles entered. Error: {}'.format(e))
    try:
        ext_data = [rnd.data[time_index] for rnd in mrf_data
                    if rnd.data[time_index] > 0 and not isinstance(rnd.rnode, str) and rnd.rnode.is_exit()]

        ext_total = sum(ext_data)
        raw_meta_data["number_of_vehicles_exited"] = ext_total
    except Exception as e:
        logger.warning('fail to calculate number of vehicles exited. Error: {}'.format(e))
    try:
        avg = statistics.mean(speed_meta_data)
        raw_meta_data["speed_average"] = avg
    except Exception as e:
        logger.warning('fail to calculate speed average. Error: {}'.format(e))
    try:
        variance = statistics.variance(speed_meta_data)
        raw_meta_data["speed_variance"] = variance
    except Exception as e:
        logger.warning('fail to calculate speed variance. Error: {}'.format(e))
    try:
        max_u = max(speed_meta_data)
        raw_meta_data["speed_max_u"] = max_u
    except Exception as e:
        logger.warning('fail to calculate speed max. Error: {}'.format(e))
    try:
        min_u = min(speed_meta_data)
        raw_meta_data["speed_min_u"] = min_u
    except Exception as e:
        logger.warning('fail to calculate speed min. Error: {}'.format(e))
    raw_meta_data["speed_difference"] = raw_meta_data["speed_max_u"] - raw_meta_data["speed_min_u"]
    return raw_meta_data


def _calculate_tt(r, prd, moe_critical_density, moe_lane_capacity, moe_congestion_threshold_speed, **kwargs):
    """

    :type r: pyticas.ttypes.Route
    :type prd: pyticas.ttypes.Period
    """
    # 1. update lane configuration according to work zone
    cloned_route = r.clone()
    updated_route = cloned_route
    if not kwargs.get('nowz', False):
        try:
            cloned_route.cfg = route_config.create_route_config(cloned_route.rnodes)
            updated_route = cloned_route
        except Exception as e:
            getLogger(__name__).warning(
                'Exception occurred while creating route config for route: {}. Error: {}'.format(r, tb.traceback(e,
                                                                                                                 f_print=False)))
        # Work zone application deactivated as we do not have enough lane closure information
        # try:
        #     updated_route = apply_workzone(cloned_route, prd)
        # except Exception as e:
        #     getLogger(__name__).warning(
        #         'Exception occurred while applying workzone for route: {}. For period: {}. Error: {}'.format(r, prd,
        #                                                                                                      tb.traceback(
        #                                                                                                          e,
        #                                                                                                          f_print=False)))

        # 2. calculate TT and Speed and VMT
    try:
        return {
            "tt": moe.travel_time(updated_route, prd),
            "speed": moe.speed(updated_route, prd),
            "vmt": moe.vmt(updated_route, prd),
            "vht": moe.vht(updated_route, prd),
            "dvh": moe.dvh(updated_route, prd),
            "lvmt": moe.lvmt(updated_route, prd, moe_critical_density=moe_critical_density,
                             moe_lane_capacity=moe_lane_capacity),
            "uvmt": moe.uvmt(updated_route, prd, moe_critical_density=moe_critical_density,
                             moe_lane_capacity=moe_lane_capacity),
            "cm": moe.cm(updated_route, prd, moe_congestion_threshold_speed=moe_congestion_threshold_speed),
            "cmh": moe.cmh(updated_route, prd, moe_congestion_threshold_speed=moe_congestion_threshold_speed),
            "acceleration": moe.acceleration(updated_route, prd),
            "raw_flow_data": total_flow_with_virtual_nodes.run(updated_route, prd),
            "raw_speed_data": speed_with_virtual_nodes.run(updated_route, prd),
            "raw_density_data": density_with_virtual_nodes.run(updated_route, prd),
            "raw_lane_data": lanes_with_virtual_nodes.run(updated_route, prd),
            "mrf": moe.mrf(updated_route, prd)
        }

    except Exception as ex:
        getLogger(__name__).warning(tb.traceback(ex))


def _route_avgs(res_list):
    """

    :type res_list: list[pyticas.ttypes.RNodeData]
    :rtype: list[float]
    """
    data_list = [res.data for res in res_list]
    imputated_data = spatial_avg.imputation(data_list)
    n_stations = len(res_list)
    n_data = len(res_list[0].prd.get_timeline())
    avgs = []
    for didx in range(n_data):
        data = [imputated_data[sidx][didx] for sidx in range(n_stations) if imputated_data[sidx][didx] >= 0]
        avgs.append(np.mean(data))
    return avgs


def _route_total(res_list):
    """

    :type res_list: list[pyticas.ttypes.RNodeData]
    :rtype: list[float]
    """
    data_list = [res.data for res in res_list]
    n_stations = len(res_list)
    n_data = len(res_list[0].prd.get_timeline())
    total_values = []
    for didx in range(n_data):
        total_values.append(
            sum([data_list[sidx][didx] for sidx in range(n_stations) if str(data_list[sidx][didx]) != '-']))
    return total_values
