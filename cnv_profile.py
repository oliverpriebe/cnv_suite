import pandas as pd
import numpy as np
from intervaltree import Interval, IntervalTree
from collections import namedtuple, deque
from random import choice
import re
import natsort


Event = namedtuple("Event", ['type', 'allele', 'cluster_num', 'cn_change'])


class CNV_Profile:

    def __init__(self, num_subclones=3, csize=None, cent_loc=None):
        if not csize:
            csize = {'1': 249250621, '2': 243199373, '3': 198022430, '4': 191154276, '5': 180915260,
                     '6': 171115067, '7': 159138663, '8': 146364022, '9': 141213431, '10': 135534747,
                     '11': 135006516, '12': 133851895, '13': 115169878, '14': 107349540, '15': 102531392,
                     '16': 90354753, '17': 81195210, '18': 78077248, '19': 59128983, '20': 63025520,
                     '21': 48129895, '22': 51304566, '23': 156040895, '24': 57227415}
        elif type(csize) != dict:
            if type(csize) == str:
                if csize[-3:] == 'bed':
                    columns = ['chr', 'start', 'len']  # three columns if bed file
                else:
                    columns = ['chr', 'len']
                csize_df = pd.read_csv(csize, sep='\t', header=None, names=columns)
            elif type(csize) == pd.DataFrame:
                csize_df = csize.copy()
            else:
                raise ValueError('csize input must be one of [None, dict, file path, pandas DataFrame]')
            csize_df.set_index('chr', inplace=True)
            csize = csize_df.to_dict()['len']
        
        if not cent_loc:
            cent_loc = {chrom: int(size / 2) for chrom, size in csize.items()}  # todo change
        elif type(cent_loc) != dict:
            if type(cent_loc) == str:
                cent_loc_df = pd.read_csv(cent_loc, sep='\t', header=None, names=['chr', 'pos'])
            elif type(cent_loc) == pd.DataFrame:
                cent_loc_df = cent_loc.copy()
                cent_loc_df.columns = ['chr', 'pos']
            else:
                raise ValueError('cent_loc input must be one of [None, dict, file path, pandas DataFrame]')
            cent_loc_df.set_index('chr', inplace=True)
            cent_loc = cent_loc_df.to_dict()['pos']

        self.cent_loc = switch_contigs(cent_loc)
        self.csize = switch_contigs(csize)

        self.event_trees = self.init_all_chrom()
        self.phylogeny = Phylogeny(num_subclones)
        self.cnv_trees = None
        self.cnv_profile_df = None
        self.phased_profile_df = None

    def init_all_chrom(self):
        """Initialize event tree dictionaries with Chromosomes containing a single haploid interval for each allele."""
        tree_dict = {}
        for chrom, size in self.csize.items():
            tree = Chromosome(chrom, size)
            tree.add_seg('haploid', 'maternal', 1, 1, 1, size)
            tree.add_seg('haploid', 'paternal', 1, 1, 1, size)
            tree_dict[chrom] = tree

        return tree_dict

    def add_cnv_events(self, arm_num, focal_num, p_whole, ratio_clonal,
                       median_focal_length=1.8 * 10**6,
                       chromothripsis=False, wgd=False):
        """Add CNV events according to criteria."""
        # add clonal events
        for _ in np.arange(arm_num * ratio_clonal):
            self.add_arm(1, p_whole)
        for _ in np.arange(focal_num * ratio_clonal):
            self.add_focal(1, median_focal_length)

        # add subclonal events
        for cluster in np.arange(2, self.phylogeny.num_subclones + 2):
            for _ in np.arange(arm_num * ratio_clonal / self.phylogeny.num_subclones):
                self.add_arm(cluster, p_whole)
            for _ in np.arange(focal_num * ratio_clonal / self.phylogeny.num_subclones):
                self.add_focal(cluster, median_focal_length)

    def add_arm(self, cluster_num, p_whole, chrom=None):
        """Add an arm level copy number event given the specifications.

        todo: don't homo-delete whole arm """
        if not chrom:
            chrom = choice(list(self.csize.keys()))
        start = 1
        end = self.csize[chrom]

        # choose arm-level vs. whole chromosome event
        if np.random.rand() > p_whole:
            if np.random.rand() > 0.5:
                start = self.cent_loc[chrom]
            else:
                end = self.cent_loc[chrom]

        # choose maternal vs. paternal
        allele = 'paternal' if np.random.rand() > 0.5 else 'maternal'

        # choose level (based on current CN and cluster number)
        pat_int, mat_int = self.calculate_cnv_lineage(chrom, start, end, cluster_num)
        desired_int = pat_int if allele == 'paternal' else mat_int
        other_int = pat_int if allele == 'maternal' else mat_int

        # if deletion:
        # - only delete so that current intervals + deletion >= -1
        # - can delete up to that value (max(1, current_intervals + 1))
        # if amplification:
        # - should probably double whatever is in current intervals
        if np.random.rand() < 0.6:  # P(deletion)
            # check for arm level deletion in other interval list
            weighted_del = 0
            for o in other_int:
                weighted_del += o.data.cn_change == 0 * (o.end - o.begin) / (end - start)
            deletion_adjust = 0 if weighted_del > 0.3 else 1

            if np.random.rand() < 0.3:  # delete fully
                for i in desired_int:
                    self.event_trees[chrom].add_seg_interval('arm', cluster_num, -i.data.cn_change * deletion_adjust, i)
            else:  # delete one copy (is there a way to delete multiple focal copies?)  - maybe get full interval tree here too to check arm vs. focal events
                current_levels = [i.data.cn_change for i in desired_int]
                desired_change = [0 if lev == 0 or deletion_adjust == 0 else -1 for lev in current_levels]
                for i, level in zip(desired_int, desired_change):
                    self.event_trees[chrom].add_seg_interval('arm', cluster_num, level, i)
        else:
            for i in desired_int:
                self.event_trees[chrom].add_seg_interval('arm', cluster_num, i.data.cn_change, i)

        # maybe return things to make CN LoH easier

    def add_focal(self, cluster_num, median_focal_length=1.8 * 10**6):
        # choose chromosome
        chrom = choice(list(self.csize.keys()))

        # choose length of event - from exponential
        focal_length_rate = median_focal_length / np.log(2)
        focal_length = np.floor(np.random.exponential(focal_length_rate)).astype(int)
        start_pos = np.random.randint(1, max(2, self.csize[chrom] - focal_length))
        end_pos = start_pos + focal_length

        # choose maternal vs. paternal
        allele = 'paternal' if np.random.rand() > 0.5 else 'maternal'

        # get current CN intervals for this branch of phylogenetic tree
        pat_int, mat_int = self.calculate_cnv_lineage(chrom, start_pos, end_pos, cluster_num)
        desired_int = pat_int if allele == 'paternal' else mat_int

        if np.random.rand() < 0.5:  # P(deletion)
            # lean towards fully deleting intervals (if there is already an amplification)
            for i in desired_int:
                curr_level = i.data.cn_change
                chosen_del = max(1, curr_level - np.random.poisson(curr_level / 10)) if curr_level != 0 else 0
                self.event_trees[chrom].add_seg_interval('focal', cluster_num, -chosen_del, i)
        else:
            # if amplification, just add equal number for entire interval unless already fully deleted
            cnv_level = np.random.poisson(0.8) + 1
            for i in desired_int:
                chosen_amp = cnv_level if i.data.cn_change != 0 else 0
                self.event_trees[chrom].add_seg_interval('focal', cluster_num, chosen_amp, i)

    def add_wgd(self):  # todo
        """

        Call add_arm for each chromosome
        :return:
        """
        pass

    def add_chromothripsis(self):  # todo
        pass

    def add_cn_loh(self):  # todo
        """

        Call add_arm or add_focal twice, once for each allele.
        :return:
        """
        pass

    def calculate_cnv_lineage(self, chrom, start, end, cluster_num):
        return self.event_trees[chrom].calc_current_cnv_lineage(start, end, cluster_num, self.phylogeny)

    def calculate_cnv_profile(self):
        cnv_trees = {}
        for chrom, interval_tree in self.event_trees.items():
            cnv_trees[chrom] = interval_tree.calc_full_cnv(self.phylogeny)

        self.cnv_trees = cnv_trees

    def calculate_df_profiles(self):
        cnv_df = []
        phasing_df = []

        for chrom, profile_tree in self.event_trees.items():  # change this todo
            cnv_df.append(profile_tree.get_cnv_df(self.cnv_trees[chrom][0], self.cnv_trees[chrom][1]))
            phasing_df.append(profile_tree.get_phased_df(self.cnv_trees[chrom][0], self.cnv_trees[chrom][1]))

        self.cnv_profile_df = pd.concat(cnv_df)
        self.phased_profile_df = pd.concat(phasing_df)

    def generate_coverage(self, purity, cov_binned, x_coverage=None, sigma=None):
        """Generate binned coverage profile based on purity and copy number profile.

        :param x_coverage: optional integer to overwrite cov_binned coverage values with Log-Normal Poisson values with lambda=x_coverage
        :param sigma: optional value for Log-Normal sigma value
        :param purity: desired purity/tumor fraction of tumor sample
        :param cov_binned: tsv file with binned coverage for genome; coverage manipulation (changing X coverage) already applied
        :return:

        Needs to take in the cnv profile and the purity as well:
        - The x_coverage is relative to a local ploidy of 2. Given the CN profile, it may be more or less than that.
        - local_coverage = x_coverage * ploidy / 2 where ploidy = pur*(mu_min+mu_maj) + (1-pur)*2

        :todo: speed-up
        """
        if not sigma:
            sigma = 1
        x_coverage_df = pd.read_csv(cov_binned, sep='\t', names=['chrom', 'start', 'end', 'coverage'],
                                    dtype={'chrom': str, 'start': int, 'end': int}, header=0)
        
        # todo change contigs to [0-9]+ from chr[0-9XY]+ in input file
        x_coverage_df = switch_contigs(x_coverage_df)

        x_coverage_df = x_coverage_df[x_coverage_df['chrom'].isin(self.csize.keys())]
        x_coverage_df['ploidy'] = x_coverage_df.apply(
            lambda x: get_average_ploidy(self.cnv_trees[x['chrom']], x['start'], x['end'], purity),
            axis=1)  # check genome bins - inclusive or exclusive (how it is now) todo

        if x_coverage:  # is it okay to apply LNP to binned coverage? todo
            dispersion_norm = np.random.normal(0, sigma, x_coverage_df.shape[0])
            binned_coverage = x_coverage * (x_coverage_df['end'] - x_coverage_df['start']) / 2
            this_chr_coverage = np.asarray([np.random.poisson(cov + np.exp(disp)) for cov, disp in
                                           zip(binned_coverage, dispersion_norm)])
            x_coverage_df['coverage'] = this_chr_coverage

        x_coverage_df['cov_adjust'] = np.floor(x_coverage_df['coverage'].values * x_coverage_df['ploidy'].values / 2)
        x_coverage_df['cov_adjust'] = x_coverage_df['cov_adjust'].astype(int)

        return x_coverage_df[['chrom', 'start', 'end', 'cov_adjust', 'coverage', 'ploidy']]

    def save_coverage_file(self, filename, purity, cov_binned_file, x_coverage=None, sigma=None):
        cov_df = self.generate_coverage(purity, cov_binned_file, x_coverage=x_coverage, sigma=sigma)
        cov_df.rename(columns={'chrom': 'chr', 'cov_adjust': 'covcorr', 'coverage': 'covraw'}).to_csv(filename, sep='\t', index=False)  # todo is this what we want (column headings)?

    def generate_snvs(self, vcf, bed, purity):
        # todo should check that the vcf header (chrs and chr lengths) match with self.chr
                
        snv_df = pd.read_csv(vcf, sep='\t', comment='#', header=None, 
                     names=['CHROM','POS','ID','REF','ALT','QUAL','FILTER','INFO','FORMAT','NA12878'])
        bed_df = pd.read_csv(bed, sep='\t', header=0, names=['CHROM', 'POS', 'DEPTH'], dtype={'CHROM': str})

        # todo change contigs to [0-9]+ from chr[0-9XY]+ in input file
        snv_df = switch_contigs(snv_df)
        bed_df = switch_contigs(bed_df)
        
        snv_df = snv_df.merge(bed_df, on=['CHROM', 'POS'])
        snv_df['ploidy'] = snv_df.apply(
            lambda x: get_average_ploidy(self.cnv_trees[x['CHROM']], x['POS'], x['POS'] + 1, purity),
            axis=1)

        snv_df['maternal_prop'] = snv_df.apply(
            lambda x: ((sorted(self.cnv_trees[x['CHROM']][1][x['POS']])[0].data.cn_change) * purity +
                      (1 - purity) ) / x['ploidy'], axis=1)

        snv_df['paternal_prop'] = snv_df.apply(
            lambda x: ((sorted(self.cnv_trees[x['CHROM']][0][x['POS']])[0].data.cn_change) * purity +
                      (1 - purity) ) / x['ploidy'], axis=1)

        snv_df['maternal_present'] = snv_df['NA12878'].apply(lambda x: x[0] == '1')
        snv_df['paternal_present'] = snv_df['NA12878'].apply(lambda x: x[2] == '1')

        snv_df['adjusted_depth'] = np.floor(snv_df['DEPTH'] * snv_df['ploidy'] / 2).astype(int)
        
        # generate phase switch profile
        # chromosome interval trees: False if phase switched
        correct_phase_interval_trees = self.generate_phase_switching()

        # calculate alt counts for each SNV
        snv_df['alt_count'] = snv_df.apply(
            lambda x: get_alt_count(x['maternal_prop'], x['paternal_prop'], x['maternal_present'],
                                    x['paternal_present'], x['adjusted_depth'],
                                    correct_phase_interval_trees[x['CHROM']][x['POS']].pop().data), axis=1)
        snv_df['ref_count'] = snv_df['adjusted_depth'] - snv_df['alt_count']

        return snv_df, correct_phase_interval_trees

    def save_hets_file(self, filename, vcf, bed, purity):
        vcf_df, _ = self.generate_snvs(vcf, bed, purity)
        vcf_df.rename(columns={'CHROM': 'CONTIG', 'POS': 'POSITION',
                                'ref_count': 'REF_COUNT', 'alt_count': 'ALT_COUNT'})[['CONTIG', 'POSITION',
                                                                                      'REF_COUNT', 'ALT_COUNT']].to_csv(filename, sep='\t', index=False)

    def generate_phase_switching(self):
        phase_switches = {}
        for chrom, size in self.csize.items():
            tree = IntervalTree()
            start = 1
            correct_phase = True
            while start < size:
                interval_len = np.floor(np.random.exponential(1e6))
                tree[start:start+interval_len] = correct_phase
                correct_phase = not correct_phase
                start += interval_len

            phase_switches[chrom] = tree

        return phase_switches


def get_alt_count(m_prop, p_prop, m_present, p_present, coverage, correct_phase):
    if not correct_phase:
        m_prop, p_prop = p_prop, m_prop
        m_present, p_present = p_present, m_present

    if m_present and p_present:
        return coverage   # add noise? as in: np.random.binomial(coverage, 0.999)
    elif not m_present and not p_present:
        return 0  # noise?
    elif m_present:
        return np.random.binomial(coverage, m_prop)
    else:
        return np.random.binomial(coverage, p_prop)


def get_average_ploidy(tree, start, end, purity):
    # need to run over paternal and maternal alleles
    def single_allele_ploidy(allele):
        intervals = allele.envelop(start, end) | allele[start] | allele[end - 1]
        interval_totals = [(min(i.end, end) - max(i.begin, start)) * i.data[3] for i in  # todo
                           intervals]
        return np.asarray(interval_totals).sum() / (end - start)
    pat_ploidy = single_allele_ploidy(tree[1])
    mat_ploidy = single_allele_ploidy(tree[0])

    return (mat_ploidy + pat_ploidy) * purity + 2 * (1 - purity)


class Chromosome:
    def __init__(self, chr_name, chr_length):
        self.chr_name = chr_name
        self.chr_length = chr_length
        self.paternal_tree = IntervalTree()
        self.maternal_tree = IntervalTree()

    def add_seg(self, type, allele, cluster_num, cn_change, start, end):
        if allele == 'paternal':
            self.paternal_tree[start:end] = Event(type, allele, cluster_num, cn_change)
        else:
            self.maternal_tree[start:end] = Event(type, allele, cluster_num, cn_change)

    def add_seg_interval(self, type, cluster_num, cn_change, interval):
        self.add_seg(type, interval.data.allele, cluster_num, cn_change, interval.begin, interval.end)

    def calc_current_cnv_lineage(self, start, end, cluster_num, phylogeny):
        lineage_clusters, _ = phylogeny.get_lineage(cluster_num)

        pat_intervals = self.paternal_tree.copy()
        pat_intervals.slice(start)
        pat_intervals.slice(end)
        pat_tree = IntervalTree()
        for i in pat_intervals.envelop(start, end):
            if i.data.cluster_num in lineage_clusters:
                pat_tree.add(i)
        pat_tree.split_overlaps()
        pat_tree.merge_overlaps(data_reducer=self.sum_levels)

        mat_intervals = self.maternal_tree.copy()
        mat_intervals.slice(start)
        mat_intervals.slice(end)
        mat_tree = IntervalTree()
        for i in mat_intervals.envelop(start, end):
            if i.data.cluster_num in lineage_clusters:
                mat_tree.add(i)
        mat_tree.split_overlaps()
        mat_tree.merge_overlaps(data_reducer=self.sum_levels)

        return pat_tree, mat_tree

    def calc_full_cnv(self, phylogeny):
        pat_tree = IntervalTree()
        for i in self.paternal_tree:
            weighted_cn = i.data.cn_change * phylogeny.ccfs[i.data.cluster_num]
            pat_tree[i.begin: i.end] = Event(i.data.type, i.data.allele, i.data.cluster_num, weighted_cn)
        pat_tree.split_overlaps()
        pat_tree.merge_overlaps(data_reducer=self.sum_levels)

        mat_tree = IntervalTree()
        for i in self.maternal_tree:
            weighted_cn = i.data.cn_change * phylogeny.ccfs[i.data.cluster_num]
            mat_tree[i.begin: i.end] = Event(i.data.type, i.data.allele, i.data.cluster_num, weighted_cn)
        mat_tree.split_overlaps()
        mat_tree.merge_overlaps(data_reducer=self.sum_levels)

        # could deliver a Chromosome (or child class) instead of just a tree
        return pat_tree, mat_tree

    def get_cnv_df(self, pat_tree, mat_tree):
        both_alleles = IntervalTree(list(pat_tree) + list(mat_tree))
        both_alleles.split_overlaps()
        both_alleles.merge_overlaps(data_reducer=self.specify_levels)
        seg_df = []
        for segment in both_alleles:
            seg_df.append([self.chr_name, segment.begin, segment.end, segment.data['major'], segment.data['minor']])

        return pd.DataFrame(seg_df, columns=['Chromosome', 'Start.bp', 'End.bp', 'major', 'minor'])

    def get_phased_df(self, pat_tree, mat_tree):
        both_alleles = IntervalTree(list(pat_tree) + list(mat_tree))
        both_alleles.split_overlaps()
        both_alleles.merge_overlaps(data_reducer=self.specify_phasing)
        seg_df = []
        for segment in both_alleles:
            seg_df.append(
                [self.chr_name, segment.begin, segment.end, segment.data['paternal'], segment.data['maternal']])

        return pd.DataFrame(seg_df, columns=['Chromosome', 'Start.bp', 'End.bp', 'paternal', 'maternal'])

    @staticmethod
    def sum_levels(old, new):
        return Event(old.type, old.allele, None, old.cn_change + new.cn_change)

    @staticmethod
    def specify_levels(old, new):
        return {'major': max(old.cn_change, new.cn_change), 'minor': min(old.cn_change, new.cn_change)}

    @staticmethod
    def specify_phasing(old, new):
        return {'paternal': old.cn_change if old.allele == 'paternal' else new.cn_change, 'maternal': old.cn_change if old.allele == 'maternal' else new.cn_change}


class Phylogeny:

    def __init__(self, num_subclones):
        """Class to represent simulated tumor phylogeny
        
        :param num_subclones: desired number of subclones
        :attribute parents: dictionary mapping clones (keys) to their parent clone (values), with 1: None representing truncal branch
        :attribute ccfs: dictionary mapping clones to their CCF
        """
        self.num_subclones = num_subclones
        self.parents, self.ccfs = self.make_phylogeny()

    def make_phylogeny(self):
        """Greedy algorithm to assign children clones in correct phylogeny based on random CCFs
        
        :return: (dict, dict) representing the parent and ccfs dictionaries"""
        ccfs = sorted(np.random.rand(self.num_subclones), reverse=True)
        ccfs = {cluster + 2: ccf for cluster, ccf in enumerate(ccfs)}
        parent_dict = {1: None}

        unassigned = deque(list(ccfs.keys()))
        parent_queue = deque([1])
        ccfs[1] = 1
        while len(unassigned) > 0:
            parent = parent_queue.popleft()
            ccf_remaining = ccfs[parent]
            for c in unassigned.copy():
                if ccfs[c] <= ccf_remaining:
                    this_cluster = c
                    unassigned.remove(c)
                    parent_queue.append(this_cluster)
                    parent_dict[this_cluster] = parent
                    ccf_remaining -= ccfs[this_cluster]

        return parent_dict, ccfs

    def get_lineage(self, node):
        """Return lineage for the specified clone
        
        :param node: index of desired clone
        :return: (list, list) representing the clones in the lineage and their respective CCFs"""
        cluster_list = []

        while node:
            cluster_list.append(node)
            node = self.parents[node]

        return cluster_list, [self.ccfs[c] for c in cluster_list]
    

def switch_contigs(input_data):
    """Return the input data with 'chr' removed from contigs and X/Y changed to 23/24.
    
    :param input_data: dict or pd.DataFrame with contig as keys or column
    :returns: dict or pd.DataFrame with altered contig names"""
    if type(input_data) == pd.DataFrame:
        contig_column_names = ['Chr', 'Chromosome', 'Chrom', 'Contig']  # defines possible column names
        contig_column_names = contig_column_names + [s.lower() for s in contig_column_names] + [s.upper() for s in contig_column_names]  # accounts for all lower/upper-case
        contig_column_names = contig_column_names + [s + 's' for s in contig_column_names]  # pluralizes column names
        column_idx = np.where([c in contig_column_names for c in input_data.columns])[0][0]  # find contig column
        column_label = input_data.columns[column_idx]
        
        input_data[column_label] = input_data[column_label].apply(lambda x: re.search('(?<=chr)[0-9XY]+|^[0-9XY]+', x).group())
        input_data.replace(to_replace={column_label:{'X':'23', 'Y':'24'}}, value=None, inplace=True)
        input_data.sort_values(column_label, key=natsort.natsort_keygen(), inplace=True)
        return input_data
    elif type(input_data) == dict:
        input_data = {re.search('(?<=chr)[0-9XY]+|^[0-9XY]+', key).group():loc for key, loc in input_data.items()}
        if 'X' in input_data.keys():
            input_data['23'] = input_data['X']
            input_data.pop('X')
        if 'Y' in input_data.keys():
            input_data['24'] = input_data['Y']
            input_data.pop('Y')
        return input_data
    else:
        raise ValueError(f'Only dictionaries and pandas DataFrames supported. Not {type(input_data)}.')
    
    
