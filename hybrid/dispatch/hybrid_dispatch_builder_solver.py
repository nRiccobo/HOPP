from typing import Union
import sys, os
from pathlib import Path
import time

import pyomo.environ as pyomo
from pyomo.network import Port, Arc
from pyomo.opt import TerminationCondition
from pyomo.util.check_units import assert_units_consistent

from hybrid.sites import SiteInfo
from hybrid.dispatch import HybridDispatch, HybridDispatchOptions, DispatchProblemState
from hybrid.clustering import Clustering

class HybridDispatchBuilderSolver:
    """Helper class for building hybrid system dispatch problem, solving dispatch problem, and simulating system
    with dispatch solution."""
    def __init__(self,
                 site: SiteInfo,
                 power_sources: dict,
                 dispatch_options: dict = None):
        """

        Parameters
        ----------
        dispatch_options :
            Contains attribute key, value pairs to change default dispatch options.
            For details see HybridDispatchOptions in hybrid_dispatch_options.py

        """

        self.site: SiteInfo = site
        self.power_sources = power_sources
        self.options = HybridDispatchOptions(dispatch_options)

        # deletes previous log file under same name
        if os.path.isfile(self.options.log_name):
            os.remove(self.options.log_name)

        self.needs_dispatch = any(item in ['battery', 'tower', 'trough'] for item in self.power_sources.keys())

        if self.needs_dispatch:
            self._pyomo_model = self._create_dispatch_optimization_model()
            self.dispatch.create_gross_profit_objective()
            # self.dispatch.initialize_parameters()
            self.dispatch.create_arcs()
            assert_units_consistent(self.pyomo_model)
            self.problem_state = DispatchProblemState()
        
        # Clustering (optional)
        self.clustering = None
        if self.options.use_clustering:
            #TODO: Add resource data for wind
            self.clustering = Clustering(power_sources.keys(), self.site.solar_resource.filename, wind_resource_data = None, price_data = self.site.elec_prices.data)
            self.clustering.n_cluster = self.options.n_clusters
            if len(self.options.clustering_weights.keys()) == 0:
                self.clustering.use_default_weights = True
            elif self.options.clustering_divisions.keys != self.options.clustering_weights.keys():
                print ('Warning: Keys in user-specified dictionaries for clustering weights and divisions do not match. Reverting to default weights/divisions')
                self.clustering.use_default_weights = True
            else:
                self.clustering.weights = self.options.clustering_weights
                self.clustering.divisions = self.options.clustering_divisions
            self.clustering.run_clustering()  # Create clusters and find exemplar days for simulation


    def _create_dispatch_optimization_model(self):
        """
        Creates monolith dispatch model
        """
        model = pyomo.ConcreteModel(name='hybrid_dispatch')
        #################################
        # Sets                          #
        #################################
        model.forecast_horizon = pyomo.Set(doc="Set of time periods in time horizon",
                                           initialize=range(self.options.n_look_ahead_periods))
        #################################
        # Blocks (technologies)         #
        #################################
        module = getattr(__import__("hybrid"), "dispatch")
        for source, tech in self.power_sources.items():
            if source == 'battery':
                tech._dispatch = self.options.battery_dispatch_class(
                    model,
                    model.forecast_horizon,
                    tech._system_model,
                    tech._financial_model,
                    include_lifecycle_count=self.options.include_lifecycle_count)
            else:
                try:
                    dispatch_class_name = getattr(module, source.capitalize() + "Dispatch")
                except AttributeError:
                    raise ValueError("Could not find {} in hybrid.dispatch module. Is {} supported in the hybrid "
                                     "dispatch model?".format(source.capitalize() + "Dispatch", source))
                tech._dispatch = dispatch_class_name(
                    model,
                    model.forecast_horizon,
                    tech._system_model,
                    tech._financial_model)

        self._dispatch = HybridDispatch(
            model,
            model.forecast_horizon,
            self.power_sources,
            self.options)
        return model

    @staticmethod
    def glpk_solve_call(pyomo_model: pyomo.ConcreteModel,
                        log_name: str = ""):

        # log_name = "annual_solve_GLPK.log"  # For debugging MILP solver
        with pyomo.SolverFactory('glpk') as solver:
            # Ref. on solver options: https://en.wikibooks.org/wiki/GLPK/Using_GLPSOL
            solver_options = {'cuts': None,
                              'presol': None,
                              # 'mostf': None,
                              # 'mipgap': 0.001,
                              'tmlim': 20
                              }
            if log_name != "":
                solver_options['log'] = "dispatch_solver.log"

            results = solver.solve(pyomo_model, options=solver_options)

        if log_name != "":
            HybridDispatchBuilderSolver.append_solve_to_log(log_name, solver_options['log'])

        if results.solver.termination_condition == TerminationCondition.infeasible:
            HybridDispatchBuilderSolver.print_infeasible_problem(pyomo_model)
        elif not results.solver.termination_condition == TerminationCondition.optimal:
            print("Warning: Dispatch problem termination condition was '"
                  + str(results.solver.termination_condition) + "'")
        return results

    def glpk_solve(self):
        return HybridDispatchBuilderSolver.glpk_solve_call(self.pyomo_model, self.options.log_name)
        
    @staticmethod
    def gurobi_ampl_solve_call(pyomo_model: pyomo.ConcreteModel,
                               log_name: str = ""):

        # Ref. on solver options: https://www.gurobi.com/documentation/9.1/ampl-gurobi/parameters.html
        with pyomo.SolverFactory('gurobi', executable='/opt/solvers/gurobi', solver_io='nl') as solver:
            solver_options = {'timelim': 30}

            if log_name != "":
                solver_options['logfile'] = "dispatch_solver.log"

            results = solver.solve(pyomo_model, options=solver_options)

        if log_name != "":
            HybridDispatchBuilderSolver.append_solve_to_log(log_name, solver_options['logfile'])

        if results.solver.termination_condition == TerminationCondition.infeasible:
            HybridDispatchBuilderSolver.print_infeasible_problem(pyomo_model)
        elif not results.solver.termination_condition == TerminationCondition.optimal:
            print("Warning: Dispatch problem termination condition was '"
                  + str(results.solver.termination_condition) + "'")
        return results

    def gurobi_ampl_solve(self):
        return HybridDispatchBuilderSolver.gurobi_ampl_solve_call(self.pyomo_model, self.options.log_name)

    @staticmethod
    def cbc_solve_call(pyomo_model: pyomo.ConcreteModel,
                       log_name: str = ""):
        # log_name = "annual_solve_CBC.log"

        # Solver options can be found by launching executable 'start cbc.exe', verbose 15, ?
        # https://coin-or.github.io/Cbc/faq.html (a bit outdated)
        solver_options = {  # 'ratioGap': 0.001,
                          'seconds': 10}

        if sys.platform == 'win32' or sys.platform == 'cygwin':
            cbc_path = Path(__file__).parent / "cbc_solver" / "cbc-win64" / "cbc"
            if log_name != "":
                print("Warning: CBC solver logging is active... This will significantly increase simulation time.")
                solver = pyomo.SolverFactory('asl:cbc', executable=cbc_path)

                solve_log = "dispatch_solver.log"
                solver_options['log'] = 2
                results = solver.solve(pyomo_model, logfile=solve_log, options=solver_options)
                HybridDispatchBuilderSolver.append_solve_to_log(log_name, solve_log)
            else:
                solver = pyomo.SolverFactory('cbc', executable=cbc_path, solver_io='nl')
                results = solver.solve(pyomo_model, options=solver_options)
        elif sys.platform == 'darwin' or sys.platform == 'linux':
            if log_name != "":
                solver_options['log'] = "dispatch_solver.log"

            solver = pyomo.SolverFactory('cbc')
            results = solver.solve(pyomo_model, options=solver_options)
            HybridDispatchBuilderSolver.append_solve_to_log(log_name, solver_options['log'])
        else:
            raise SystemError('Platform not supported ', sys.platform)

        if results.solver.termination_condition == TerminationCondition.infeasible:
            HybridDispatchBuilderSolver.print_infeasible_problem(pyomo_model)
        elif not results.solver.termination_condition == TerminationCondition.optimal:
            print("Warning: Dispatch problem termination condition is '"
                  + str(results.solver.termination_condition) + "'")
        return results

    def cbc_solve(self):
        return HybridDispatchBuilderSolver.cbc_solve_call(self.pyomo_model, self.options.log_name)

    @staticmethod
    def mindtpy_solve_call(pyomo_model: pyomo.ConcreteModel,
                           log_name: str = None):
        # FIXME: This does not work!
        solver = pyomo.SolverFactory('mindtpy')

        if log_name is not None:
            solver_options = {'log': 'dispatch_instance.log'}

        results = solver.solve(pyomo_model,
                               mip_solver='glpk',
                               nlp_solver='ipopt',
                               tee=True)

        if log_name is not None:
            HybridDispatchBuilderSolver.append_solve_to_log(log_name, solver_options['log'])

        if results.solver.termination_condition == TerminationCondition.infeasible:
            HybridDispatchBuilderSolver.print_infeasible_problem(pyomo_model)
        return results

    @staticmethod
    def append_solve_to_log(log_name: str, solve_log: str):
        # Appends single problem instance log to annual log file
        fin = open(solve_log, 'r')
        data = fin.read()
        fin.close()

        ann_log = open(log_name, 'a+')
        ann_log.write("=" * 50 + "\n")
        ann_log.write(data)
        ann_log.close()

    @staticmethod
    def print_infeasible_problem(model: pyomo.ConcreteModel):
        original_stdout = sys.stdout
        with open('infeasible_instance.txt', 'w') as f:
            sys.stdout = f
            print('\n' + '#' * 20 + ' Model Parameter Values ' + '#' * 20 + '\n')
            HybridDispatchBuilderSolver.print_all_parameters(model)
            print('\n' + '#' * 20 + ' Model Blocks Display ' + '#' * 20 + '\n')
            HybridDispatchBuilderSolver.display_all_blocks(model)
            sys.stdout = original_stdout
        raise ValueError("Dispatch optimization model is infeasible.\n"
                         "See 'infeasible_instance.txt' for parameter values.")

    @staticmethod
    def print_all_parameters(model: pyomo.ConcreteModel):
        param_list = list()
        block_list = list()
        for param_object in model.component_objects(pyomo.Param, active=True):
            name_to_print = param_object.getname()
            parent_block = param_object.parent_block().parent_component()
            block_name = parent_block.getname()
            if (name_to_print not in param_list) or (block_name not in block_list):
                block_list.append(block_name)
                param_list.append(name_to_print)
                print("\nParent Block Name: ", block_name)
                print("Parameter: ", name_to_print)
                for index in parent_block.index_set():
                    val_to_print = pyomo.value(getattr(parent_block[index], param_object.getname()))
                    print("\t", index, "\t", val_to_print)

    @staticmethod
    def display_all_blocks(model: pyomo.ConcreteModel):
        for block_object in model.component_objects(pyomo.Block, active=True):
            for index in block_object.index_set():
                block_object[index].display()

    def simulate(self):
        # Dispatch Optimization Simulation with Rolling Horizon
        # Solving the year in series
        ti = list(range(0, self.site.n_timesteps, self.options.n_roll_periods))
        self.dispatch.initialize_parameters()

        if self.clustering is None:
            for i, t in enumerate(ti):
                if self.options.is_test_start_year or self.options.is_test_end_year:
                    if (self.options.is_test_start_year and i < 5) or (self.options.is_test_end_year and i > 359):
                        start_time = time.time()
                        self.simulate_with_dispatch(t)
                        sim_w_dispath_time = time.time()
                        print('Day {} dispatch optimized.'.format(i))
                        print("      %6.2f seconds required to simulate with dispatch" % (sim_w_dispath_time - start_time))
                    else:
                        continue
                        # TODO: can we make the csp and battery model run with heuristic dispatch here?
                        #  Maybe calling a simulate_with_heuristic() method
                else:
                    self.simulate_with_dispatch(t)
        else:
            for j in range(self.clustering.clusters['n_cluster']):
                time_start, time_stop = self.clustering.get_sim_start_end_times(j)
                battery_soc = self.clustering.battery_soc_heuristic(j) if 'battery' in self.power_sources.keys() else None

                # Set CSP initial states (need to do this prior to update_time_series_parameters() or update_initial_conditions(), both pull from the stored plant state)
                for tech in ['trough', 'tower']:
                    if tech in self.power_sources.keys():
                        self.power_sources[tech].plant_state = self.power_sources[tech].set_initial_plant_state()  # Reset to default initial state
                        csp_soc = self.clustering.csp_soc_heuristic(j, solar_multiple = None)
                        self.power_sources[tech].set_tes_soc(csp_soc)  

                self.simulate_with_dispatch(time_start, self.clustering.ndays+1, battery_soc, n_initial_sims = 1)  

            # After exemplar simulations, update to full annual generation array for dispatchable technologies
            for tech in ['battery', 'trough', 'tower']:
                if tech in self.power_sources.keys():
                    gen = self.power_sources[tech].generation_profile
                    self.power_sources[tech].generation_profile = list(self.clustering.compute_annual_array_from_cluster_exemplar_data(gen))


    def simulate_with_dispatch(self,
                               start_time: int,
                               n_days: int = 1,
                               initial_soc: float = None,
                               n_initial_sims: int = 0):
        # this is needed for clustering effort
        update_dispatch_times = list(range(start_time,
                                           start_time + n_days * self.site.n_periods_per_day,
                                           self.options.n_roll_periods))

        for i, sim_start_time in enumerate(update_dispatch_times):
            # Update battery initial state of charge
            if 'battery' in self.power_sources.keys():
                self.power_sources['battery'].dispatch.update_dispatch_initial_soc(initial_soc=initial_soc)
                initial_soc = None

            for model in self.power_sources.values():
                if model.system_capacity_kw == 0:
                    continue
                model.dispatch.update_time_series_parameters(sim_start_time)

            # TODO: this is not a good way to do this... This won't work with CSP addition...
            if 'heuristic' in self.options.battery_dispatch:
                self.battery_heuristic()
                # TODO: we could just run the csp model without dispatch here
            else:
                # Solve dispatch model
                if self.options.solver == 'glpk':
                    solver_results = self.glpk_solve()       # TODO: need to condition for other non-convex model
                elif self.options.solver == 'cbc':
                    solver_results = self.cbc_solve()
                else:
                    raise ValueError("{} is not a supported solver".format(self.options.solver))

                self.problem_state.store_problem_metrics(solver_results, start_time, n_days,
                                                         self.dispatch.objective_value)
            
            store_outputs = True
            battery_sim_start_time = sim_start_time
            if i < n_initial_sims:
                store_outputs = False
                battery_sim_start_time = None

            # simulate using dispatch solution
            if 'battery' in self.power_sources.keys():
                self.power_sources['battery'].simulate_with_dispatch(self.options.n_roll_periods,
                                                                     sim_start_time=battery_sim_start_time)

            if 'trough' in self.power_sources.keys():
                self.power_sources['trough'].simulate_with_dispatch(self.options.n_roll_periods,
                                                                    sim_start_time=sim_start_time,
                                                                    store_outputs = store_outputs)
            if 'tower' in self.power_sources.keys():
                self.power_sources['tower'].simulate_with_dispatch(self.options.n_roll_periods,
                                                                   sim_start_time=sim_start_time,
                                                                   store_outputs = store_outputs)

    def battery_heuristic(self):
        tot_gen = [0.0]*self.options.n_look_ahead_periods
        if 'pv' in self.power_sources.keys():
            pv_gen = self.power_sources['pv'].dispatch.available_generation
            tot_gen = [pv + gen for pv, gen in zip(pv_gen, tot_gen)]
        if 'wind' in self.power_sources.keys():
            wind_gen = self.power_sources['wind'].dispatch.available_generation
            tot_gen = [wind + gen for wind, gen in zip(wind_gen, tot_gen)]

        grid_limit = self.power_sources['grid'].dispatch.transmission_limit

        if 'one_cycle' in self.options.battery_dispatch:
            # Get prices for one cycle heuristic
            prices = self.power_sources['grid'].dispatch.electricity_sell_price
            self.power_sources['battery'].dispatch.prices = prices

        self.power_sources['battery'].dispatch.set_fixed_dispatch(tot_gen, grid_limit)

    @property
    def pyomo_model(self) -> pyomo.ConcreteModel:
        return self._pyomo_model

    @property
    def dispatch(self) -> HybridDispatch:
        return self._dispatch





