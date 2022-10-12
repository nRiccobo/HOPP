import os
import sys
sys.path.append('')
from dotenv import load_dotenv
import pandas as pd
import json
from hybrid.sites import SiteInfo
from hybrid.sites import flatirons_site as sample_site
from hybrid.keys import set_developer_nrel_gov_key
# from plot_reopt_results import plot_reopt_results
# from run_reopt import run_reopt
from examples.H2_Analysis.hopp_for_h2 import hopp_for_h2
from examples.H2_Analysis.run_h2a import run_h2a as run_h2a
from examples.H2_Analysis.simple_dispatch import SimpleDispatch
from examples.H2_Analysis.simple_cash_annuals import simple_cash_annuals
import examples.H2_Analysis.run_h2_PEM as run_h2_PEM
import numpy as np
import numpy_financial as npf
from lcoe.lcoe import lcoe as lcoe_calc
import matplotlib.pyplot as plt
import warnings
from pathlib import Path
warnings.filterwarnings("ignore")

import hopp_tools
import inputs_py
import copy 
import plot_results

"""
Perform a LCOH analysis for an offshore wind + Hydrogen PEM system

Missing Functionality:
1. Figure out H2A Costs or ammortize cost/kw electrolyzer figure and add opex

~1. Offshore wind site locations and cost details (4 sites, $1300/kw capex + BOS cost which will come from Orbit Runs)~

2. Cost Scaling Based on Year (Have Weiser et. al report with cost scaling for fixed and floating tech, will implement)
3. Cost Scaling Based on Plant Size (Shields et. Al report)
4. Integration Required:
* Pressure Vessel Model~
* HVDC Model 
* Pipeline Model

5. Model Development Required:
- Floating Electrolyzer Platform
"""

#Set API key
load_dotenv()
NREL_API_KEY = os.getenv("NREL_API_KEY")
set_developer_nrel_gov_key('NREL_API_KEY')  # Set this key manually here if you are not setting it using the .env

#Step 1: User Inputs for scenario
resource_year = 2013
atb_years = [
            2022,
            # 2025,
            # 2030,
            # 2035
            ]
policy = {
    'option 1': {'Wind ITC': 0, 'Wind PTC': 0, "H2 PTC": 0},
    # 'option 2': {'Wind ITC': 26, 'Wind PTC': 0, "H2 PTC": 0},
    # 'option 3': {'Wind ITC': 0, 'Wind PTC': 0.003, "H2 PTC": 0},
    # 'option 4': {'Wind ITC': 0, 'Wind PTC': 0.026, "H2 PTC": 0},
    # 'option 5': {'Wind ITC': 0, 'Wind PTC': 0.003, "H2 PTC": 0.6},
    'option 6': {'Wind ITC': 0, 'Wind PTC': 0.026, "H2 PTC": 3},
}

sample_site['year'] = resource_year
useful_life = 30
critical_load_factor = 1
run_reopt_flag = False
custom_powercurve = True
storage_used = True
battery_can_grid_charge = True
grid_connected_hopp = False
interconnection_size_mw = 1000
electrolyzer_size = 1000

# which plots to show
plot_power_production = True
plot_battery = True
plot_grid = True
plot_h2 = True
plot_desal = True
plot_wind = True
plot_hvdcpipe = True
plot_hvdcpipe_lcoh = True
turbine_name = [
                #'2022ATB_12MW',
                #'2022ATB_15MW',
                '2022ATB_18MW'
                ]
h2_model ='Simple'  
# h2_model = 'H2A'

scenario = dict()
kw_continuous = electrolyzer_size * 1000
load = [kw_continuous for x in
        range(0, 8760)]  # * (sin(x) + pi) Set desired/required load profile for plant

scenario_choice = 'Offshore Wind-H2 Analysis'
site_selection = [
                'Site 1',
                # 'Site 2',
                # 'Site 3',
                # 'Site 4'
                ]
parent_path = os.path.abspath('')
results_dir = parent_path + '/examples/H2_Analysis/results/'


#Site lat and lon will be set by data loaded from Orbit runs
discount_rate = 0.07
forced_sizes = True
force_electrolyzer_cost = True
forced_wind_size = 1000
forced_solar_size = 0
forced_storage_size_mw = 0
forced_storage_size_mwh = 0
solar_cost_kw = 9999
storage_cost_kw = 250
storage_cost_kwh = 240
debt_equity_split = 60

# Enable Ability to purchase/sell electricity to/from grid. Price Defined in $/kWh
# sell_price = 0.01
# buy_price = 0.01
sell_price = False
buy_price = False

# ORBIT financial information
orbit_path = ('examples/H2_Analysis/OSW_H2_sites_turbines_and_costs.xlsx')
xl = pd.ExcelFile(orbit_path)

save_outputs_dict = inputs_py.establish_save_output_dict()
save_all_runs = list()

for i in policy:
    for atb_year in atb_years:
        for site_location in site_selection:
            for turbine_model in turbine_name:
                
                # set policy values
                scenario = hopp_tools.set_policy_values(scenario, policy, i)
                print(scenario['Wind PTC'])

                # set turbine values
                scenario = hopp_tools.set_turbine_model(turbine_model, scenario, parent_path)

                scenario['Useful Life'] = useful_life

                # financials
                scenario = hopp_tools.set_financial_info(scenario, debt_equity_split, discount_rate)

                # set electrolyzer information
                electrolyzer_capex_kw, time_between_replacement =  hopp_tools.set_electrolyzer_info(atb_year)

                # Extract Scenario Information from ORBIT Runs
                # Load Excel file of scenarios
                # OSW sites and cost file including turbines 8/16/2022 

                # site info
                path = ('examples/H2_Analysis/OSW_H2_sites_turbines_and_costs.xlsx')
                xl = pd.ExcelFile(path)
                site_df, sample_site = hopp_tools.set_site_info(xl, turbine_model, site_location, sample_site)
                site_name = site_df['Representative region']
                fixed_or_floating_wind = site_df['Substructure technology']
                site = SiteInfo(sample_site, hub_height=scenario['Tower Height'])

                #Assign Orbit results to scenario cost details
                total_capex = site_df['Total CapEx']
                wind_cost_kw = copy.deepcopy(total_capex)
                wind_om_cost_kw = site_df['OpEx, $/kW-yr']
                wind_net_cf = site_df['Assumed NCF']

                # set wind financials
                new_wind_cost_kw, new_wind_om_cost_kw, new_wind_net_cf = hopp_tools.set_turbine_financials(turbine_model, 
                                                                                                            fixed_or_floating_wind,
                                                                                                            atb_year,
                                                                                                            wind_om_cost_kw,
                                                                                                            wind_net_cf,
                                                                                                            parent_path)

                #Plot Wind Data to ensure offshore data is sound
                wind_data = site.wind_resource._data['data']
                wind_speed = [W[2] for W in wind_data]
                plot_results.plot_wind_results(wind_data, site_name, site_df['Representative coordinates'], results_dir, plot_wind)

                #Plot Wind Cost Contributions
                # Plot a nested pie chart of results
                plot_results.plot_pie(site_df, site_name, turbine_model, results_dir)
                
                # Run HOPP
                combined_pv_wind_power_production_hopp, energy_shortfall_hopp, combined_pv_wind_curtailment_hopp, hybrid_plant, wind_size_mw, solar_size_mw, lcoe = \
                    hopp_tools.run_HOPP(scenario,
                                sample_site,
                                forced_sizes,
                                forced_solar_size,
                                forced_wind_size,
                                forced_storage_size_mw,
                                forced_storage_size_mwh,
                                wind_cost_kw, 
                                solar_cost_kw, 
                                storage_cost_kw, 
                                storage_cost_kwh,
                                kw_continuous, 
                                load,
                                custom_powercurve,
                                electrolyzer_size,
                                wind_om_cost_kw)

                #Step 4: Plot HOPP Results
                plot_results.plot_HOPP(combined_pv_wind_power_production_hopp,
                                        energy_shortfall_hopp,
                                        combined_pv_wind_curtailment_hopp,
                                        load,
                                        results_dir,
                                        site_name,
                                        atb_year,
                                        turbine_model,
                                        hybrid_plant,
                                        plot_power_production)

                #Step 5: Run Simple Dispatch Model
                combined_pv_wind_storage_power_production_hopp, battery_SOC, battery_used, excess_energy = \
                    hopp_tools.run_battery(energy_shortfall_hopp, combined_pv_wind_curtailment_hopp, combined_pv_wind_power_production_hopp)
                
                plot_results.plot_battery_results(combined_pv_wind_curtailment_hopp, 
                         energy_shortfall_hopp,
                         combined_pv_wind_storage_power_production_hopp,
                         combined_pv_wind_power_production_hopp,
                         battery_SOC,
                         battery_used,
                         results_dir,
                         site_name,atb_year,turbine_model,
                         load,
                         plot_battery)

                

                # grid information
                cost_to_buy_from_grid, profit_from_selling_to_grid, energy_to_electrolyzer = hopp_tools.grid(combined_pv_wind_storage_power_production_hopp,
                                                                                     sell_price,
                                                                                     excess_energy,
                                                                                     buy_price,
                                                                                     kw_continuous,
                                                                                     plot_grid)

                #Step 6: Run the H2_PEM model
                H2_Results, H2A_Results, electrical_generation_timeseries = hopp_tools.run_H2_PEM_sim(hybrid_plant,
                                                                                                        energy_to_electrolyzer,
                                                                                                        scenario,
                                                                                                        wind_size_mw,
                                                                                                        solar_size_mw,
                                                                                                        electrolyzer_size,
                                                                                                        kw_continuous,
                                                                                                        electrolyzer_capex_kw,
                                                                                                        lcoe)

                plot_results.plot_h2_results(H2_Results, 
                                            electrical_generation_timeseries,
                                            results_dir,
                                            site_name,atb_year,turbine_model,
                                            load,
                                            plot_h2)

                #Step 6b: Run desal model
                desal_capex, desal_opex = hopp_tools.desal_model(H2_Results, 
                                                                electrolyzer_size, 
                                                                electrical_generation_timeseries, 
                                                                useful_life)

                # compressor model
                compressor, compressor_results = hopp_tools.compressor_model()

                #Pressure Vessel Model Example
                storage_input, storage_output = hopp_tools.pressure_vessel()

                # pipeline model
                total_h2export_system_cost, opex_pipeline, dist_to_port_value = hopp_tools.pipeline(site_df, 
                                                                                H2_Results, 
                                                                                useful_life, 
                                                                                storage_input)
                
                #Pipeline vs HVDC cost
                total_export_system_cost_kw, total_export_system_cost = hopp_tools.pipeline_vs_hvdc(site_df, wind_size_mw, total_h2export_system_cost)

                # plot HVDC vs pipe 
                plot_results.plot_hvdcpipe(total_export_system_cost,
                                            total_h2export_system_cost,
                                            site_name,
                                            atb_year,
                                            dist_to_port_value,
                                            results_dir)

                #*DANGER: Need to make sure this step doesnt have knock-on effects*
                # Replace export system cost with pipeline cost
                #new_wind_cost_kw = wind_cost_kw - total_export_system_cost_kw + pipeline_cost_kw
                new_wind_cost_kw = wind_cost_kw - total_export_system_cost_kw + total_h2export_system_cost/(wind_size_mw*1000)
                print("Wind Cost was ${0:,.0f}/kW and is now ${1:.0f}/kW".format(wind_cost_kw, new_wind_cost_kw))

                # Include Pipeline O&M cost to Fixed O&M 
                new_wind_om_cost_kw = wind_om_cost_kw + opex_pipeline/(wind_size_mw*1000)
                print("OpexPipe")
                print("Wind O&M was ${0:,.0f}/kW-yr and is now ${1:.2f}/kW-yr".format(wind_om_cost_kw, new_wind_om_cost_kw))

                # Run HOPP again to provide wind capital costs in pipeline scenario
                combined_pv_wind_power_production_hopp_pipeline, energy_shortfall_hopp_pipeline, combined_pv_wind_curtailment_hopp_pipeline, hybrid_plant_pipeline, wind_size_mw, solar_size_mw, lcoe_pipeline = \
                    hopp_tools.run_HOPP(scenario,
                                sample_site,
                                forced_sizes,
                                forced_solar_size,
                                forced_wind_size,
                                forced_storage_size_mw,
                                forced_storage_size_mwh,
                                new_wind_cost_kw, # this is the new variable
                                solar_cost_kw, 
                                storage_cost_kw, 
                                storage_cost_kwh,
                                kw_continuous, 
                                load,
                                custom_powercurve,
                                electrolyzer_size,
                                new_wind_om_cost_kw) # this is the new variable

                print("HOPP run for pipeline scenario")

                # Step 6.5: Intermediate financial calculation

                LCOH_cf_method_wind, LCOH_cf_method_wind_pipeline, LCOH_cf_method_solar, LCOH_cf_method_h2_costs, LCOH_cf_method_operating_costs, LCOH_cf_method_desal_costs, total_elec_production, lifetime_h2_production, gut_check_h2_cost_kg, LCOH_cf_method = \
                    hopp_tools.calculate_financials(electrical_generation_timeseries,
                                                hybrid_plant,
                                                hybrid_plant_pipeline,
                                                H2A_Results,
                                                H2_Results,
                                                desal_opex,
                                                cost_to_buy_from_grid,
                                                profit_from_selling_to_grid,
                                                useful_life,
                                                atb_year,
                                                scenario,
                                                h2_model,
                                                desal_capex,
                                                wind_cost_kw,
                                                solar_cost_kw,
                                                discount_rate,
                                                solar_size_mw,
                                                electrolyzer_size,
                                                results_dir,
                                                site_name,
                                                turbine_model,
                                                scenario_choice)

                # Step 7: Plot Results
                
                # create data
                #x = ['HVDC', 'Pipeline']
                
                # plot bars in stack manner
                if plot_hvdcpipe_lcoh:
                    plt.figure(figsize=(9,6))
                    barx = ['HVDC', 'Pipeline']
                    plt.bar(barx, [LCOH_cf_method_wind,LCOH_cf_method_wind_pipeline], color='blue')
                    plt.bar(barx, LCOH_cf_method_solar, bottom=[LCOH_cf_method_wind,LCOH_cf_method_wind_pipeline], color='orange')
                    plt.bar(barx, LCOH_cf_method_h2_costs, bottom =[(LCOH_cf_method_wind + LCOH_cf_method_solar), (LCOH_cf_method_wind_pipeline + LCOH_cf_method_solar)], color='g')
                    plt.bar(barx, LCOH_cf_method_operating_costs, bottom=[(LCOH_cf_method_wind + LCOH_cf_method_solar + LCOH_cf_method_h2_costs),(LCOH_cf_method_wind_pipeline + LCOH_cf_method_solar + LCOH_cf_method_h2_costs)], color='y')
                    plt.bar(barx, LCOH_cf_method_desal_costs, bottom=(LCOH_cf_method_wind + LCOH_cf_method_solar + LCOH_cf_method_h2_costs + LCOH_cf_method_operating_costs), color='k')

                    plt.ylabel("LCOH")
                    plt.legend(["Wind", "Solar", "H2", "Operating Costs", "Desal"])
                    plt.title("Levelized Cost of hydrogen - Cost Contributors\n {}\n {}\n {} ptc".format(site_name,atb_year,turbine_model))
                    plt.savefig(os.path.join(results_dir,'LCOH Barchart_{}_{}_{}.jpg'.format(site_name,atb_year,turbine_model)),bbox_inches='tight')
                    # plt.show()

                print_results = False
                print_h2_results = True
                # save_outputs_dict = inputs_py.save_the_things()
                # save_all_runs.append(save_outputs_dict)
                # save_outputs_dict = inputs_py.establish_save_output_dict()

                if print_results:
                    # ------------------------- #
                    #TODO: Tidy up these print statements
                    print("Future Scenario: {}".format(scenario['Scenario Name']))
                    print("Wind Cost per KW: {}".format(scenario['Wind Cost KW']))
                    print("PV Cost per KW: {}".format(scenario['Solar Cost KW']))
                    print("Storage Cost per KW: {}".format(scenario['Storage Cost kW']))
                    print("Storage Cost per KWh: {}".format(scenario['Storage Cost kWh']))
                    print("Wind Size built: {}".format(wind_size_mw))
                    print("PV Size built: {}".format(solar_size_mw))
                    print("Storage Size built: {}".format(forced_storage_size_mw))
                    print("Storage Size built: {}".format(forced_storage_size_mwh))
                    print("Levelized cost of Electricity (HOPP): {}".format(lcoe))
                    print("Total Yearly Electrical Output: {}".format(total_elec_production))
                    print("Total Yearly Hydrogen Production: {}".format(H2_Results['hydrogen_annual_output']))
                    #print("Levelized Cost H2/kg (new method - no operational costs)".format(h_lcoe_no_op_cost))
                    print("Capacity Factor of Electrolyzer: {}".format(H2_Results['cap_factor']))

                if print_h2_results:
                    print('Total Lifetime H2(kg) produced: {}'.format(lifetime_h2_production))
                    print("Gut-check H2 cost/kg: {}".format(gut_check_h2_cost_kg))
                #     print("h_lcoe: ", h_lcoe)
                    print("LCOH CF Method (doesn't include grid electricity cost if used)", LCOH_cf_method)
                   # print("Levelized cost of H2 (electricity feedstock) (HOPP): {}".format(
                    #     H2_Results['feedstock_cost_h2_levelized_hopp']))
                    # print("Levelized cost of H2 (excl. electricity) (H2A): {}".format(H2A_Results['Total Hydrogen Cost ($/kgH2)']))
                    # print("Total unit cost of H2 ($/kg) : {}".format(H2_Results['total_unit_cost_of_hydrogen']))
                    # print("kg H2 cost from net cap cost/lifetime h2 production (HOPP): {}".format(
                    #     H2_Results['feedstock_cost_h2_via_net_cap_cost_lifetime_h2_hopp']))

                    #Step 9: Summarize Results
                    print('For a {}MW Offshore Wind Plant with {}MW electrolyzer located at {} \n (average wind speed {}m/s) in {}, with a Wind CAPEX cost of {},\n and an Electrolyzer cost of {}$/kW:\n The levelized cost of hydrogen was {} /kg '.
                                format(forced_wind_size,electrolyzer_size,site_name,np.average(wind_speed),atb_year,site_df['Total CapEx'],electrolyzer_capex_kw,LCOH_cf_method))

                    print("LCOH CF Method (doesn't include grid electricity cost if used)", LCOH_cf_method)
                    

# save_outputs = True
# if save_outputs:
#     #save_outputs_dict_df = pd.DataFrame(save_all_runs)
#     save_all_runs_df = pd.DataFrame(save_all_runs)
#     save_all_runs_df.to_csv(os.path.join(results_dir, "H2_Analysis_OSW_All.csv"))


print('Done')

