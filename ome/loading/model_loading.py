# -*- coding: utf-8 -*-
# This code is primarily a merger of theseus.models by @zakandrewking and LoadTheseus by @jslu9
from ome import base, settings, components, timing
from ome.models import *
from ome.loading import component_loading


import cobra
import cobra.io
from cobra.core.Formula import Formula
import os
from os.path import join, abspath, dirname
import re
import cPickle as pickle
import hashlib
from sqlalchemy import create_engine, Table, MetaData, update, func
missinglist = open('MissingGeneList.txt', 'w')

data_path = settings.data_directory

metaboliteIdDict = {}
reactionIdDict = {}
def get_model_list():
    """Get the models that are available, as SBML, in ome_data/models"""
    return [x.replace('.xml', '').replace('.mat', '') for x in
            os.listdir(join(settings.data_directory, 'models'))
            if '.xml' in x or '.mat' in x]

def check_for_model(name):
    """Check for model, case insensitive, and ignore periods and underscores"""
    def min_name(n):
        return n.lower().replace('.','').replace(' ','').replace('_','')
    for x in get_model_list():
        if min_name(name)==min_name(x):
            return x
    return None

# the regex to separate the base id, the chirality ('_L') and the compartment ('_c')
reg = re.compile(r'(.*?)(?:(.*[^_])_([LDSR]))?[_\(\[]([a-z])[_\)\]]?$')
def id_for_new_id_style(old_id, is_metabolite=False, new_id_style='cobrapy'):
    """ Get the new style id"""

    def join_parts(the_id, the_compartment):
        if (new_id_style.lower()=='cobrapy'):
            if the_compartment:
                the_id = the_id+'_'+the_compartment
            the_id = the_id.replace('-', '__')
        elif (new_id_style.lower()=='simpheny'):
            if the_compartment and is_metabolite:
                the_id = the_id+'['+the_compartment+']'
            elif the_compartment:
                the_id = the_id+'('+the_compartment+')'
            the_id = the_id.replace('__', '-')
        else:
            raise Exception('Invalid id style')
        return the_id

    # separate the base id, the chirality ('_L') and the compartment ('_c')
    m = reg.match(old_id)
    if m is None:
        # still change the underscore/dash
        new_id = join_parts(old_id, None)
    elif m.group(2) is None:
        new_id = join_parts(m.group(1), m.group(4))
    else:
        # if the chirality is not joined by two underscores, then fix that
        a = "__".join(m.groups()[1:3])
        new_id = join_parts(a, m.group(4))

    # deal with inconsistent notation of (sec) vs. [sec] in iJO1366 versions
    new_id = new_id.replace('[sec]', '_sec_').replace('(sec)', '_sec_')

    return new_id

def convert_ids(model, new_id_style):
    """Converts metabolite and reaction ids to the new style. Style options:

    cobrapy: EX_lac__L_e
    simpheny: EX_lac-L(e)

    """
    # loop through the ids:
    #metaboliteIdDict = {}
    #reactionIdDict = {}
    # this code comes from cobra.io.sbml
    # legacy_ids add special characters to the names again
    for metabolite in model.metabolites:
        new_id = fix_legacy_id(metabolite.id, use_hyphens=False)
        metaboliteIdDict[new_id] = metabolite.id
        metabolite.id = new_id
        
    model.metabolites._generate_index()
    for reaction in model.reactions:
        new_id = fix_legacy_id(reaction.id, use_hyphens=False)
        reactionIdDict[new_id] = reaction.id
        reaction.id = new_id
    model.reactions._generate_index()
    # remove boundary metabolites (end in _b and only present in exchanges) . Be
    # sure to loop through a static list of ids so the list does not get
    # shorter as the metabolites are deleted
    for metabolite_id in [str(x) for x in model.metabolites]:
        metabolite = model.metabolites.get_by_id(metabolite_id)
        if not metabolite.id.endswith("_b"):
            continue
        for reaction in list(metabolite._reaction):
            if reaction.id.startswith("EX_"):
                metabolite.remove_from_model()
                break
    model.metabolites._generate_index()

    # separate ids and compartments, and convert to the new_id_style
    for reaction in model.reactions:
        new_id = id_for_new_id_style(reaction.id, new_id_style=new_id_style)
        reactionIdDict[new_id] = reaction.id
        reaction.id = new_id
    model.reactions._generate_index()
    for metabolite in model.metabolites:
        new_id = id_for_new_id_style(metabolite.id, is_metabolite=True, new_id_style=new_id_style)
        metaboliteIdDict[new_id] = metabolite.id
        metabolite.id = new_id
    model.metabolites._generate_index()

    return model

def parse_model(name, id_style='cobrapy'):
    """Load a model, and give it a particular id style"""

    # check for model
    name = check_for_model(name)
    print name
    if not name:
        raise Exception('Could not find model')

    # load the model pickle, or, if not, the sbml
    try:
        with open(join(settings.data_directory, 'models', 'model_pickles', name+'.pickle'), 'r') as f:
            model = pickle.load(f)
    except:
        try:
            model = cobra.io.read_sbml_model(join(settings.data_directory, 'models', name+'.xml'))
        except:
            model = cobra.io.load_matlab_model(join(settings.data_directory, 'models', name+'.mat'))

        pickle_dir = join(settings.data_directory, 'models', 'model_pickles')

        if not os.path.isdir(pickle_dir):
            os.mkdir(pickle_dir)

        with open(join(pickle_dir, name+'.pickle'), 'w') as f:
            pickle.dump(model, f)

    # convert the ids
    model = convert_ids(model, id_style)

    # extract metabolite formulas from names (e.g. for iAF1260)
    model = get_formulas_from_names(model)

    # turn off carbon sources
    model = turn_off_carbon_sources(model)

    return model

def get_formulas_from_names(model):
    reg = re.compile(r'.*_([A-Za-z0-9]+)$')
    for metabolite in model.metabolites:
        if (metabolite.formula is not None
            and metabolite.formula.formula!=''
            and metabolite.formula.formula is not None): continue
        m = reg.match(metabolite.name)
        if m:
            metabolite.formula = Formula(m.group(1))
    return model

def turn_off_carbon_sources(model):
    for reaction in model.reactions:
        if 'EX_' not in str(reaction): continue
        if carbons_for_exchange_reaction(reaction) > 0:
            reaction.lower_bound = 0
    return model

def setup_model(model, substrate_reactions, aerobic=True, sur=10, max_our=10,
                id_style='cobrapy', fix_iJO1366=False):
    """Set up the model with environmntal parameters.

    model: a cobra model
    substrate_reactions: A single reaction id, list of reaction ids, or dictionary with reaction
    ids as keys and max substrate uptakes as keys. If a list or single id is
    given, then each substrate will be limited to /sur/
    aerobic: True or False
    sur: substrate uptake rate. Ignored if substrate_reactions is a dictionary.
    max_our: Max oxygen uptake rate.
    id_style: 'cobrapy' or 'simpheny'.

    """
    if id_style=='cobrapy': o2 = 'EX_o2_e'
    elif id_style=='simpheny': o2 = 'EX_o2(e)'
    else: raise Exception('Invalid id_style')

    if isinstance(substrate_reactions, dict):
        for r, v in substrate_reactions.iteritems():
            model.reactions.get_by_id(r).lower_bound = -abs(v)
    elif isinstance(substrate_reactions, list):
        for r in substrate_reactions:
            model.reactions.get_by_id(r).lower_bound = -abs(sur)
    elif isinstance(substrate_reactions, str):
        model.reactions.get_by_id(substrate_reactions).lower_bound = -abs(sur)
    else: raise Exception('bad substrate_reactions argument')

    if aerobic:
        model.reactions.get_by_id(o2).lower_bound = -abs(max_our)
    else:
        model.reactions.get_by_id(o2).lower_bound = 0

    # model specific setup
    if str(model)=='iJO1366' and aerobic==False:
        for r in ['CAT', 'SPODM', 'SPODMpp']:
            model.reactions.get_by_id(r).lower_bound = 0
            model.reactions.get_by_id(r).upper_bound = 0
    if fix_iJO1366 and str(model)=='iJO1366':
        for r in ['ACACT2r']:
            model.reactions.get_by_id(r).upper_bound = 0
        print 'made ACACT2r irreversible'

    # TODO hydrogen reaction for ijo

    if str(model)=='iMM904' and aerobic==False:
        necessary_ex = ['EX_ergst(e)', 'EX_zymst(e)', 'EX_hdcea(e)',
                        'EX_ocdca(e)', 'EX_ocdcea(e)', 'EX_ocdcya(e)']
        for r in necessary_ex:
            rxn = model.reactions.get_by_id(r)
            rxn.lower_bound = -1000
            rxn.upper_bound = 1000

    return model

def turn_on_subsystem(model, subsytem):
    raise NotImplementedError()
    for reaction in model.reactions:
        if reaction.subsystem.strip('_') == subsytem.strip('_'):
            reaction.lower_bound = -1000 if reaction.reversibility else 0
            reaction.upper_bound = 1000
    return model

def carbons_for_exchange_reaction(reaction):
    if len(reaction._metabolites) > 1:
        raise Exception('%s not an exchange reaction' % str(reaction))

    metabolite = reaction._metabolites.iterkeys().next()
    try:
        return metabolite.formula.elements['C']
    except KeyError:
        return 0
    # match = re.match(r'C([0-9]+)', str(metabolite.formula))
    # try:
    #     return int(match.group(1))
    # except AttributeError:
    #     return 0

def add_pathway(model, new_metabolites, new_reactions, subsystems, bounds,
                check_mass_balance=False, ignore_repeats=False):
    """Add a pathway to the model. Reversibility defaults to reversible (1).

    new_metabolites: e.g. { 'ggpp_c': {'formula': 'C20H33O7P2', 'name': 'name'},
                            'phyto_c': {'formula': 'C40H64'}},
                            'lyco_c': {'formula': 'C40H56'},
                            'lyco_e': {'formula': 'C40H56'} }
    new_reactions: e.g. { 'FPS': { 'ipdp_c': -2,
                                   'ppi_c': 1,
                                   'grdp_c': 1 },
                          'CRTE': { 'ipdp_c': -1,
                                    'frdp_c': -1,
                                    'ggpp_c': 1,
                                    'ppi_c': 1 } }
    subsystems: e.g. { 'FPS': 'Lycopene production',
                       'CRTE': 'Lycopene production' }
    bound: e.g. { 'FPS': (0, 0),
                  'CRTE': (0, 1000) }

    """

    for k, v in new_metabolites.iteritems():
        formula = Formula(v['formula']) if 'formula' in v else None
        name = v['name'] if 'name' in v else None
        m = cobra.Metabolite(id=k, formula=formula, name=name)
        try:
            model.add_metabolites([m])
        except Exception as err:
            if (not ignore_repeats or
                "already in the model" not in str(err)):
                raise(err)

    for name, mets in new_reactions.iteritems():
        r = cobra.Reaction(name=name)
        m_obj = {}
        for k, v in mets.iteritems():
            m_obj[model.metabolites.get_by_id(k)] = v
        r.add_metabolites(m_obj)
        if bounds and (name in bounds):
            r.lower_bound, r.upper_bound = bounds[name]
        else:
            r.upper_bound = 1000
            r.lower_bound = -1000
        if subsystems and (name in subsystems):
            r.subsystem = subsystems[name]
        try:
            model.add_reaction(r)
        except Exception as err:
            if (not ignore_repeats or
                "already in the model" not in str(err)):
                raise(err)
        if check_mass_balance and 'EX_' not in name:
            balance = model.reactions.get_by_id(name).check_mass_balance()
            if balance != []:
                raise Exception('Bad balance: %s' % str(balance))
    return model

def fix_legacy_id(id, use_hyphens=False):
    id = id.replace('_DASH_', '__')
    id = id.replace('_FSLASH_', '/')
    id = id.replace('_BSLASH_', "\\")
    id = id.replace('_LPAREN_', '(')
    id = id.replace('_LSQBKT_', '[')
    id = id.replace('_RSQBKT_', ']')
    id = id.replace('_RPAREN_', ')')
    id = id.replace('_COMMA_', ',')
    id = id.replace('_PERIOD_', '.')
    id = id.replace('_APOS_', "'")
    id = id.replace('&amp;', '&')
    id = id.replace('&lt;', '<')
    id = id.replace('&gt;', '>')
    id = id.replace('&quot;', '"')
    if use_hyphens:
        id = id.replace('__', '-')
    else:
        id = id.replace("-", "__")
    return id
    
def split_compartment(component_id):
    """Split the metabolite bigg_id into a metabolite and a compartment id.
    
    Arguments
    ---------
    
    component_id: the bigg_id of the metabolite.
    
    """
    match = re.search(r'_[a-z][a-z0-9]?$', component_id)
    if match is None:
        raise Exception("No compartment found for %s" % component_id)
    met = component_id[0:match.start()]
    compartment = component_id[match.start()+1:]
    return met, compartment

class IndependentObjects:

    def loadGenes(self, modellist, session):
        for model in modellist:
            for gene in model.genes:
                if not session.query(Gene).filter(Gene.name == gene.id).count():
                    geneObject = Gene(locus_id = gene.id)
                    session.add(geneObject)

    def loadModel(self, model, session, genome_id, first_created, pubmedId):
        if session.query(Model).filter_by(bigg_id=model.id).count():
            print "model already uploaded"
            return
        else:
            
            modelObject = Model(bigg_id = model.id, first_created = first_created, genome_id = genome_id, notes = '')
            session.add(modelObject)
            if session.query(base.Publication).filter(base.Publication.pmid == pubmedId).count() == 0: 
                p = base.Publication(pmid = pubmedId)
                session.add(p)
                publication = session.query(base.Publication).filter(base.Publication.pmid == pubmedId).first()
                pm = base.PublicationModel(publication_id= publication.id, model_id = modelObject.id)
                session.add(pm)
            else:
                publication = session.query(base.Publication).filter(base.Publication.pmid == pubmedId).first()
                pm = base.PublicationModel(publication_id= publication.id, model_id = modelObject.id)
                session.add(pm)
                
    def parse_id(id):
        id_string = str(id)
        return id_string.replace("{","").replace("}","").replace('[', '').replace(']', '').replace("&apos;","").replace("'","")
        
    def loadComponents(self, modellist, session):
        for model in modellist:
            for component in model.metabolites:
                try:
                    metabolite = session.query(Metabolite).filter(Metabolite.name == split_compartment(component.id)[0])
                except Exception as e:
                    print "%s. In model %s" (e, model.id)
                    continue
                    
                #metabolite = session.query(Metabolite).filter(Metabolite.kegg_id == component.notes.get("KEGGID")[0])
                if not metabolite.count():
                    try:
                        if isinstance( component.notes.get("KEGGID"), list):
                            _kegg_id = parse_id(component.notes.get("KEGGID"))
                        else:
                            _kegg_id = parse_id(component.notes.get("KEGGID"))
                    except: _kegg_id = None
                    try:
                        if isinstance( component.notes.get("CASNUMBER"), list):
                            _cas_number = parse_id(component.notes.get("CASNUMBER"))
                        else: 
                            _cas_number = parse_id(component.notes.get("CASNUMBER"))
                    except: _cas_number = None
                    try: 
                        formula = component.notes.get("FORMULA")
                    except: formula = None
                    try:
                        if isinstance( component.notes.get("BRENDA"), list):
                            _brenda = parse_id(component.notes.get("BRENDA"))
                        else: 
                            _brenda = parse_id(component.notes.get("BRENDA"))
                    except: _brenda = None
                    try:
                        if isinstance( component.notes.get("SEED"), list):
                            _seed = parse_id(component.notes.get("SEED"))
                        else: 
                            _seed = parse_id(component.notes.get("SEED"))
                    except: _seed = None
                    try:
                        if isinstance( component.notes.get("CHEBI"), list):
                            _chebi = parse_id(component.notes.get("CHEBI"))
                        else:
                            _chebi = parse_id(component.notes.get("CHEBI"))
                    except: _chebi = None
                    try:
                        if isinstance( component.notes.get("METACYC"), list):
                            _metacyc = parse_id(component.notes.get("METACYC")) 
                        else:
                            _metacyc = parse_id(component.notes.get("METACYC"))
                    except: _metacyc = None
                    try:
                        if isinstance( component.notes.get("UPA"), list):
                            _upa = parse_id(component.notes.get("UPA"))
                        else:
                            _upa = parse_id(component.notes.get("UPA"))
                    except: _upa = None
                    
                    if component.notes.get("FORMULA1") != None:
                        _formula = component.notes.get("FORMULA1")
                    else:
                        _formula = component.formula
                    metaboliteObject = Metabolite(name = split_compartment(component.id)[0],
                                                  long_name = component.name,
                                                  kegg_id = _kegg_id,
                                                  cas_number = _cas_number,
                                                  seed = _seed, 
                                                  chebi = _chebi, 
                                                  metacyc = _metacyc,
                                                  upa = _upa, 
                                                  brenda = _brenda,
                                                  formula = str(_formula),
                                                  flag = bool(_kegg_id))
                    session.add(metaboliteObject)
                else:
                    linkouts = ['KEGGID', 'CAS_NUMBER', 'SEED', 'METACYC', 'CHEBI', 'BRENDA', 'UPA']
                    for linkout in linkouts:
                        if (component.notes.get(linkout) is not None and getattr(metabolite, linkout) is not None):
                            setattr(metabolite, linkout, parse_id(component.notes.get(linkout)))
                        
    def loadReactions(self , modellist, session):
        for model in modellist:
            for reaction in model.reactions:
                reaction_string = ""
                metabolitelist = [(x, x.id) for x in reaction._metabolites.keys()]
                mlist = sorted(metabolitelist, key=lambda met:met[1])
                for key in mlist:
                    reaction_string += str(reaction._metabolites[key[0]])+str(key[1])
                #m = hashlib.md5()
                #m.update(reaction_string)
                #reaction_hash = m.hexdigest()
                if not session.query(Reaction).filter(Reaction.name == reaction.id).count():
                    reactionObject = Reaction(name = reaction.id, long_name = reaction.name, notes = '', reaction_hash = hash(reaction_string))
                    session.add(reactionObject)

    def loadCompartments(self, modellist, session):
        compartments_all = set()
        for model in modellist:
            for component in model.metabolites:
                if component.id is not None:
                    compartments_all.add(split_compartment(component.id)[1])
            for symbol in compartments_all:
                if not session.query(Compartment).filter(Compartment.name == symbol).count():
                    compartmentObject = Compartment(name = symbol)
                    session.add(compartmentObject)
"""  
                    if len(component.id.split('_'))>1:
                        
                                             
                        if not session.query(Compartment).filter(Compartment.name == split_compartment(component.id)[1]).count():
                            compartmentObject = Compartment(name = split_compartment(component.id)[1])
                            session.add(compartmentObject)
                        
                    else:
                        if not session.query(Compartment).filter(Compartment.name == 'none').count():
                            compartmentObject = Compartment(name = 'none')
                            session.add(compartmentObject)
"""           
class DependentObjects:
    def loadModelGenes(self, modellist, session):
        for model in modellist:
            for gene in model.genes:
                if gene.id != 's0001':
                    modelquery = session.query(Model).filter(Model.bigg_id == model.id).first()
                    chromosomequery = session.query(Chromosome).filter(Chromosome.genome_id == modelquery.genome_id).all()
                    if len(chromosomequery) == 0:
                        print "no chromosome"
                    for chrom in chromosomequery:
                        if session.query(Gene).filter(Gene.locus_id == gene.id).filter(Gene.chromosome_id == chrom.id).first() != None:
                            genequery = session.query(Gene).filter(Gene.locus_id == gene.id).filter(Gene.chromosome_id == chrom.id).first()
                            if not session.query(ModelGene).join(Gene).filter(ModelGene.model_id == modelquery.id).filter(ModelGene.gene_id == genequery.id).count():
                                object = ModelGene(model_id = modelquery.id, gene_id = genequery.id)
                                session.add(object)
                                session.commit() 
                        elif session.query(Gene).filter(Gene.name == gene.id).filter(Gene.chromosome_id == chrom.id).first() != None:
                            genequery = session.query(Gene).filter(Gene.name == gene.id).filter(Gene.chromosome_id == chrom.id).first()
                        
                            if not session.query(ModelGene).join(Gene).filter(ModelGene.model_id == modelquery.id).filter(ModelGene.gene_id == genequery.id).count():
                                object = ModelGene(model_id = modelquery.id, gene_id = genequery.id)
                                session.add(object)
                                session.commit()
                        elif session.query(Gene).filter(Gene.name == gene.id.split('.')[0]).filter(Gene.chromosome_id == chrom.id).first() != None:
                            genequery = session.query(Gene).filter(Gene.name == gene.id.split('.')[0]).filter(Gene.chromosome_id == chrom.id).first()
                        
                            if not session.query(ModelGene).join(Gene).filter(ModelGene.model_id == modelquery.id).filter(ModelGene.gene_id == genequery.id).count():
                                object = ModelGene(model_id = modelquery.id, gene_id = genequery.id)
                                session.add(object)
                                session.commit()
                        else:
                            synonymquery = session.query(Synonyms).filter(Synonyms.synonym == gene.id.split(".")[0]).filter(Synonyms.type == 'gene').all()
                            if len(synonymquery) != 0:
                                print gene.id
                                for syn in synonymquery:
                                    genecheck = session.query(Gene).filter(Gene.id == syn.ome_id).first()
                                    if genecheck is not None:
                                        if not session.query(ModelGene).join(Gene).filter(ModelGene.model_id == modelquery.id).filter(ModelGene.gene_id == genecheck.id).count():
                                            object = ModelGene(model_id = modelquery.id, gene_id = syn.ome_id)
                                            session.add(object)
                                            session.commit()
                                        else:
                                            print "no model gene found" 

                                        if modelquery.bigg_id == "RECON1" or modelquery.bigg_id == "iMM1415":
                                            genequery = session.query(Gene).filter(Gene.id == syn.ome_id).first()
                                            genequery.locus_id = gene.id
                                    else:
                                        print syn.ome_id
                            else:
                                ome_gene = {}
                                ome_gene['locus_id'] = gene.id
                                ome_gene['name'] = gene.name
                                ome_gene['leftpos'] = gene.locus_start
                                ome_gene['rightpos'] = gene.locus_end
                                ome_gene['chromosome_id'] = chrom.id
                                ome_gene['long_name'] = gene.name
                                ome_gene['strand'] = gene.strand
                                ome_gene['info'] = str(gene.annotation)
                                ome_gene['mapped_to_genbank'] = False
                                geneObject = Gene(**ome_gene)
                                session.add(geneObject)
                                geneQuery = session.query(Gene).filter(Gene.locus_id == gene.id).filter(Gene.name == gene.name).filter(Gene.leftpos == gene.locus_start).filter(Gene.rightpos == gene.locus_end).filter(Gene.chromosome_id == chrom.id).filter(Gene.strand == gene.strand).filter(Gene.mapped_to_genbank == False).one()
                                object = ModelGene(model_id = modelquery.id, gene_id = geneQuery.id)
                                session.add(object)
                                session.commit()                            
                            """
                            elif session.query(Synonyms).filter(Synonyms.synonym == gene.id).filter(Synonyms.type == 'gene').count():
                                synonymquery = session.query(Synonyms).filter(Synonyms.synonym == gene.id).filter(Synonyms.type == 'gene').all()
                                for syn in synonymquery:
                                    genecheck = session.query(Gene).filter(Gene.id == syn.ome_id).first()
                                    if genecheck:
                                
                                        if not session.query(ModelGene).join(Gene).filter(ModelGene.model_id == modelquery.id).filter(ModelGene.gene_id == genecheck.id).count():
                                            object = ModelGene(model_id = modelquery.id, gene_id = syn.ome_id)
                                            session.add(object)
                                            session.commit()

                                        if modelquery.bigg_id == "RECON1":
                                            genequery = session.query(Gene).filter(Gene.id == syn.ome_id).first()
                                            genequery.locus_id = gene.id
                                    else:
                                        print syn.ome_id
                            else:
                                print " create gene"
                                ome_gene = {}
                                ome_gene['locus_id'] = gene.id
                                ome_gene['name'] = gene.name
                                ome_gene['leftpos'] = gene.locus_start
                                ome_gene['rightpos'] = gene.locus_end
                                ome_gene['chromosome_id'] = chrom.id
                                ome_gene['long_name'] = gene.name
                                ome_gene['strand'] = gene.strand
                                ome_gene['info'] = str(gene.annotation)
                                ome_gene['mapped_to_genbank'] = False
                                #gene = session.get_or_create(components.Gene, **ome_gene)
                                gene = Gene(**ome_gene)
                                session.add(gene)
                                
                                print "gene not from genbank was created"
                                object = ModelGene(model_id = modelquery.id, gene_id = gene.id)
                                session.add(object)
                                session.commit()
                                #statement = gene.id + 'is missing in the genbank file. model: ' + model.id +'\n'
                                #missinglist.write(statement)
                            """   
                            

    def loadCompartmentalizedComponent(self, modellist, session):
        for model in modellist:
            for metabolite in model.metabolites:
                identifier = session.query(Compartment).filter(Compartment.name == split_compartment(metabolite.id)[1]).first()
                m = session.query(Metabolite).filter(Metabolite.name == split_compartment(metabolite.id)[0]).first()
                componentCheck = session.query(CompartmentalizedComponent).filter(CompartmentalizedComponent.component_id == m.id).filter(CompartmentalizedComponent.compartment_id == identifier.id)
                if not componentCheck.count():
                    object = CompartmentalizedComponent(component_id = m.id, compartment_id = identifier.id)
                    session.add(object)

    def loadModelCompartmentalizedComponent(self, modellist, session):
        for model in modellist:
            for metabolite in model.metabolites:
                componentquery = session.query(Metabolite).filter(Metabolite.name == split_compartment(metabolite.id)[0]).first()
                #componentquery = session.query(Metabolite).filter(Metabolite.kegg_id == metabolite.notes.get("KEGGID")[0]).first()
                compartmentquery = session.query(Compartment).filter(Compartment.name == split_compartment(metabolite.id)[1]).first()
                compartmentalized_component_query = session.query(CompartmentalizedComponent).filter(CompartmentalizedComponent.component_id == componentquery.id).filter(CompartmentalizedComponent.compartment_id == compartmentquery.id).first()
                modelquery = session.query(Model).filter(Model.bigg_id == model.id).first()
                if modelquery is None:
                    print "model query is none", model.id
                    #from IPython import embed; embed()

                if compartmentalized_component_query is None:
                    print "compartmentalized_component_query is none", metabolite.id
                if not session.query(ModelCompartmentalizedComponent).filter(ModelCompartmentalizedComponent.compartmentalized_component_id == compartmentalized_component_query.id).filter(ModelCompartmentalizedComponent.model_id == modelquery.id).count():
                    object = ModelCompartmentalizedComponent(model_id = modelquery.id, compartmentalized_component_id = compartmentalized_component_query.id, compartment_id = compartmentquery.id)
                    session.add(object)


    def loadModelReaction(self, modellist, session):
        for model in modellist:
            for reaction in model.reactions:
                
                reactionquery = session.query(Reaction).filter(Reaction.name == reaction.id).first()
                modelquery = session.query(Model).filter(Model.bigg_id == model.id).first()
                if reactionquery != None:
                    if not session.query(ModelReaction).filter(ModelReaction.reaction_id == reactionquery.id).filter(ModelReaction.model_id == modelquery.id).count():
                        object = ModelReaction(reaction_id = reactionquery.id, model_id = modelquery.id, name = reaction.id, upperbound = reaction.upper_bound, lowerbound = reaction.lower_bound, gpr = reaction.gene_reaction_rule)
                        session.add(object)


    def loadGPRMatrix(self, modellist, session):
        for model in modellist:
            for reaction in model.reactions:
                for gene in reaction._genes:
                    if gene.id != 's0001':

                        model_query = session.query(Model).filter(Model.bigg_id == model.id).first()
                        model_gene_query = session.query(ModelGene).join(Gene).filter(Gene.locus_id == gene.id).filter(ModelGene.model_id == model_query.id).first()

                        if model_gene_query != None:
                            model_reaction_query = session.query(ModelReaction).filter(ModelReaction.name == reaction.id).filter(ModelReaction.model_id == model_query.id).first()
                            if not session.query(GPRMatrix).filter(GPRMatrix.model_gene_id == model_gene_query.id).filter(GPRMatrix.model_reaction_id == model_reaction_query.id).count():
                                object = GPRMatrix(model_gene_id = model_gene_query.id, model_reaction_id = model_reaction_query.id)
                                session.add(object)
                        else:
                            model_gene_query = session.query(ModelGene).join(Gene).filter(Gene.name == gene.id).filter(ModelGene.model_id == model_query.id).first()
                            if model_gene_query != None:
                                
                                model_reaction_query = session.query(ModelReaction).filter(ModelReaction.name == reaction.id).filter(ModelReaction.model_id == model_query.id).first()
                                if not session.query(GPRMatrix).filter(GPRMatrix.model_gene_id == model_gene_query.id).filter(GPRMatrix.model_reaction_id == model_reaction_query.id).count():

                                    object = GPRMatrix(model_gene_id = model_gene_query.id, model_reaction_id = model_reaction_query.id)
                                    session.add(object)
                            else:
                                synonymquery = session.query(Synonyms).filter(Synonyms.synonym == gene.id.split(".")[0]).first()
                                if synonymquery != None:
                                    if synonymquery.ome_id != None:
                                        model_gene_query = session.query(ModelGene).filter(ModelGene.gene_id == synonymquery.ome_id).filter(ModelGene.model_id == model_query.id).first()
                                        model_reaction_query = session.query(ModelReaction).filter(ModelReaction.name == reaction.id).filter(ModelReaction.model_id == model_query.id).first()
                                        
                                        if model_reaction_query and model_gene_query:
                                            if not session.query(GPRMatrix).filter(GPRMatrix.model_gene_id == model_gene_query.id).filter(GPRMatrix.model_reaction_id == model_reaction_query.id).count():

                                                object = GPRMatrix(model_gene_id = model_gene_query.id, model_reaction_id = model_reaction_query.id)
                                                session.add(object)
                                        else:
                                            print "model reaction or model gene was not found " + str(reaction.id) + " " + str(synonymquery.ome_id)
                                    else:
                                        print "ome id is null " + synonymquery.ome_id
                                else:
                                    print "mistake", gene.id, reaction.id

    def loadReactionMatrix(self, modellist, session):
        for model in modellist:
            for reaction in model.reactions:
                reactionquery = session.query(Reaction).filter(Reaction.name == reaction.id).first()
                for metabolite in reaction._metabolites:

                    componentquery = session.query(Metabolite).filter(Metabolite.name == split_compartment(metabolite.id)[0]).first()
                    #componentquery = session.query(Metabolite).filter(Metabolite.kegg_id == metabolite.notes.get("KEGGID")[0]).first()
                    compartmentquery = session.query(Compartment).filter(Compartment.name == split_compartment(metabolite.id)[1]).first()
                    compartmentalized_component_query = session.query(CompartmentalizedComponent).filter(CompartmentalizedComponent.component_id == componentquery.id).filter(CompartmentalizedComponent.compartment_id == compartmentquery.id).first()
                    if not session.query(ReactionMatrix).filter(ReactionMatrix.reaction_id == reactionquery.id).filter(ReactionMatrix.compartmentalized_component_id == compartmentalized_component_query.id).count():
                        for stoichKey in reaction._metabolites.keys():
                            if str(stoichKey) == metabolite.id:
                                stoichiometryobject = reaction._metabolites[stoichKey]
                        object = ReactionMatrix(reaction_id = reactionquery.id, compartmentalized_component_id = compartmentalized_component_query.id, stoichiometry = stoichiometryobject)
                        session.add(object)

    def loadEscher(self, session):
        m = models.parse_model('iJO1366')
        for reaction in m.reactions:
            escher = Escher_Map(bigg_id = reaction.id, category = "reaction", model_name = m.id)
            session.add(escher)
    
    def loadModelCount(self, model, session):
        for model_id in session.query(Model.id).filter(Model.bigg_id == model.id):
            metabolite_count = (session
                                .query(ModelCompartmentalizedComponent.id)
                                .filter(ModelCompartmentalizedComponent.model_id == model_id)
                                .count())
            reaction_count = (session.query(ModelReaction.id)
                            .filter(ModelReaction.model_id == model_id)
                            .count())
            gene_count = (session.query(ModelGene.id)
                            .filter(ModelGene.model_id == model_id)       
                            .count())
            mc = ModelCount(model_id = model_id, gene_count = gene_count, metabolite_count = metabolite_count, reaction_count = reaction_count)
            session.add(mc)
    
    def loadOldIdtoSynonyms(self, session):
        for mkey in metaboliteIdDict.keys():
            ome_synonym = {'type':'metabolite'}
            m = session.query(Metabolite).filter(Metabolite.name == split_compartment(mkey)[0]).first()
            if m is not None:
                ome_synonym['ome_id'] = m.id
                ome_synonym['synonym'] = metaboliteIdDict[mkey]
            
                if session.query(base.DataSource).filter(base.DataSource.name=="old id").count():                   
                    data_source_query = session.query(base.DataSource).filter(base.DataSource.name=="old id").first()
                    data_source_id = data_source_query.id
                else:
                    data_source = base.DataSource(name="old id")
                    session.add(data_source)
                    session.flush()
                    data_source_id = data_source.id
                ome_synonym['synonym_data_source_id'] = data_source_id
                if not session.query(base.Synonyms).filter(base.Synonyms.ome_id == m.id).filter(base.Synonyms.synonym == metaboliteIdDict[mkey]).filter(base.Synonyms.type == 'metabolite').first():
                    synonym = base.Synonyms(**ome_synonym)
                    session.add(synonym)
                    
        ome_synonym = {}
        for rkey in reactionIdDict.keys():
            ome_synonym = {'type':'reaction'}
            r = session.query(Reaction).filter(Reaction.name == rkey).first()
            if r is not None:
                ome_synonym['ome_id'] = r.id
                ome_synonym['synonym'] = reactionIdDict[rkey]
            
                if session.query(base.DataSource).filter(base.DataSource.name=="old id").count():
                    data_source_query = session.query(base.DataSource).filter(base.DataSource.name=="old id").first()
                    data_source_id = data_source_query.id
                else:
                    data_source = base.DataSource(name="old id")
                    session.add(data_source)
                    session.flush()
                    data_source_id = data_source.id
                ome_synonym['synonym_data_source_id'] = data_source_id
                if not session.query(base.Synonyms).filter(base.Synonyms.ome_id == r.id).filter(base.Synonyms.synonym == reactionIdDict[rkey]).filter(base.Synonyms.type == 'reaction').first():
                    synonym = base.Synonyms(**ome_synonym)
                    session.add(synonym)
               
@timing
def load_model(model_id, genome_id, model_creation_timestamp, pmid):
    with create_Session() as session:

        try: genome = session.query(base.Genome).filter_by(bioproject_id=genome_id).one()
        except:
            print 'Genbank file %s for model %s was not uploaded' % (genome_id, model_id)
            return
        if session.query(Model).filter_by(bigg_id=model_id).count():
            print "model already uploaded"
            return       
        model = parse_model(model_id)
        IndependentObjects().loadModel(model, session, genome.id, model_creation_timestamp, pmid)
        IndependentObjects().loadComponents([model], session)
        IndependentObjects().loadCompartments([model], session)
        DependentObjects().loadCompartmentalizedComponent([model], session)
        IndependentObjects().loadReactions([model], session)
        DependentObjects().loadModelGenes([model], session)
        DependentObjects().loadModelCompartmentalizedComponent([model], session)
        DependentObjects().loadModelReaction([model], session)
        DependentObjects().loadGPRMatrix([model], session)
        DependentObjects().loadReactionMatrix([model], session)
        DependentObjects().loadModelCount(model, session)
        DependentObjects().loadOldIdtoSynonyms(session)
        #DependentObjects().loadEscher(session)



