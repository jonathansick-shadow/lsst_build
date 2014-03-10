#############################################################################
# Preparer

import os, os.path
import sys
import eups, eups.tags
import hashlib
import shutil
import time
import re
import pipes
import subprocess
import collections

import tsort

from .git import Git, GitError

class VerDB(object):
	def __init__(self, dbrepo):
		self.dbrepo = dbrepo
		self.git = Git(dbrepo)

	def get_version(self):
		pass

class Product(object):
	def __init__(self, name, sha1, version, dependencies):
		self.name = name
		self.sha1 = sha1
		self.version = version
		self.dependencies = dependencies

class Manifest(object):
	def __init__(self, productsList, buildID=None):
		self.buildID = buildID
		self.products = productsList

	def toFile(self, fileObject):
		# Write out the build manifest
		print '# %-23s %-41s %-30s' % ("product", "SHA1", "Version")
		print 'BUILD=%s' % self.buildID
		for prod in self.products:
			print '%-25s %-41s %-40s %s' % (prod.name, prod.sha1, prod.version, ','.join(dep.name for dep in prod.dependencies))

	@staticmethod
	def fromFile(fileObject):
		pass;

	@staticmethod
	def fromProductDict(productDict):
		# Topologically sort the list of products
		deps = [ (dep.name, prod.name) for prod in productDict.itervalues() for dep in prod.dependencies ];
		topoSortedProductNames = tsort.tsort(deps)

		# Append top-level products with no dependencies
		_p = set(topoSortedProductNames)
		for name in set(productDict.iterkeys()):
			if name not in _p:
				topoSortedProductNames.append(name)

		return Manifest( [productDict[name] for name in topoSortedProductNames], None)

class ProductFetcher(object):
	def __init__(self, build_dir, repository_patterns, refs, no_fetch):
		self.build_dir = os.path.abspath(build_dir)
		self.refs = refs
		self.repository_patterns = repository_patterns.split('|')
		self.no_fetch = no_fetch

	def _origin_candidates(self, product):
		""" Expand repository_patterns into URLs. """
		data = { 'product': product }
		return [ pat % data for pat in self.repository_patterns ]

	def fetch(self, product):
		""" Mirror the product repository into the build directory and extract the appropriate ref """

		t0 = time.time()
		sys.stderr.write("%20s: " % product)

		productdir = os.path.join(self.build_dir, product)
		git = Git(productdir)

		# verify the URL of origin hasn't changed
		if os.path.isdir(productdir):
			origin = git('config', '--get', 'remote.origin.url')
			if origin not in self._origin_candidates(product):
				shutil.rmtree(productdir)

		# clone
		if not os.path.isdir(productdir):
			for url in self._origin_candidates(product):
				if not Git.clone(url, productdir, return_status=True)[1]:
					break
			else:
				raise Exception("Failed to clone product '%s' from any of the offered repositories" % product)

		# update from origin
		if not self.no_fetch:
			# the line below should be equivalent to:
			#     git.fetch("origin", "--force", "--prune")
			#     git.fetch("origin", "--force", "--tags")
			# but avoids the overhead of two (possibly remote) git calls.
			git.fetch("-fup", "origin", "+refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*")

		# find a ref that matches, checkout it
		for ref in self.refs:
			sha1, _ = git.rev_parse("-q", "--verify", "refs/remotes/origin/" + ref, return_status=True)
			#print ref, "branch=", sha1
			branch = sha1 != ""
			if not sha1:
				sha1, _ = git.rev_parse("-q", "--verify", "refs/tags/" + ref + "^0", return_status=True)
			if not sha1:
				sha1, _ = git.rev_parse("-q", "--verify", "__dummy-g" + ref, return_status=True)
			if not sha1:
				continue

			git.checkout("--force", ref)

			if branch:
				# profiling showed that git-pull took a lot of time; since
				# we know we want the checked out branch to be at the remote sha1
				# we'll just reset it
				git.reset("--hard", sha1)

			#print "HEAD=", git.rev_parse("HEAD")
			assert(git.rev_parse("HEAD") == sha1)
			break
		else:
			raise Exception("None of the specified refs exist in product '%s'" % product)

		# clean up the working directory (eg., remove remnants of
		# previous builds)
		git.clean("-d", "-f", "-q")

		print >>sys.stderr, " ok (%.1f sec)." % (time.time() - t0)
		return ref, sha1

class VersionMaker(object):
	def __init__(self, sha_abbrev_len):
		self.sha_abbrev_len = sha_abbrev_len
		pass

	def _rebuild_suffix(self, dependencies):
		""" Return a hash of the sorted list of printed (dep_name, dep_version) tuples """
		m = hashlib.sha1()
		for dep in sorted(dependencies, lambda a, b: cmp(a.name, b.name)):
			s = '%s\t%s\n' % (dep.name, dep.version)
			m.update(s)

		suffix = m.hexdigest()[:self.sha_abbrev_len]
		return suffix

	def version(self, productdir, ref, dependencies):
		""" Return a standardized XXX+YYY EUPS version, that includes the dependencies. """
		q = pipes.quote
		cmd ="cd %s && pkgautoversion %s" % (q(productdir), q(ref))
		ver = subprocess.check_output(cmd, shell=True).strip()

		if dependencies:
			deps_sha1 = self._rebuild_suffix(dependencies)
			return "%s+%s" % (ver, deps_sha1)
		else:
			return ver

class ExclusionResolver(object):
	def __init__(self, exclusion_patterns):
		self.exclusions = [
			(re.compile(dep_re), re.compile(prod_re)) for (dep_re, prod_re) in exclusion_patterns
		]

	def is_excluded(self, dep, product):
		""" Check if dependency 'dep' is excluded for product 'product' """
		try:
			rc = self._exclusion_regex_cache
		except AttributeError:
			rc = self._exclusion_regex_cache = dict()

		if product not in rc:
			rc[product] = [ dep_re for (dep_re, prod_re) in self.exclusions if prod_re.match(product) ]

		for dep_re in rc[product]:
			if dep_re.match(dep):
				return True

		return False

	@staticmethod
	def fromFile(fileObject):
		exclusion_patterns = []

		for line in fileObject:
			line = line.strip()
			if not line or line.startswith("#"):
				continue

			exclusion_patterns.append(line.split()[:2])

		return ExclusionResolver(exclusion_patterns)

def nextAvailableEupsBuildTag(tags):
	# Generate a build ID

	btre = re.compile('^b[0-9]+$')
	btags = [ 0 ]
	btags += [ int(tag[1:]) for tag in tags.getTagNames() if btre.match(tag) ]
	tag = "b%s" % (max(btags) + 1)

	return tag

class BuildDirectoryConstructor(object):
	def __init__(self, build_dir, eups, product_fetcher, version_maker, exclusion_resolver):
		self.build_dir = os.path.abspath(build_dir)

		self.eups = eups
		self.product_fetcher = product_fetcher
		self.version_maker = version_maker
		self.exclusion_resolver = exclusion_resolver

	def _add_product_tree(self, products, productName):
		if productName in products:
			return products[productName]

		# Mirror the product into the build directory (clone or git-pull it)
		ref, sha1 = self.product_fetcher.fetch(productName)

		# Parse the table file to discover dependencies
		dependencies = []
		productdir = os.path.join(self.build_dir, productName)
		table_fn = os.path.join(productdir, 'ups', '%s.table' % productName)
		if os.path.isfile(table_fn):
			# Prepare the non-excluded dependencies
			for dep in eups.table.Table(table_fn).dependencies(self.eups):
				(dprod, doptional) = dep[0:2]

				# skip excluded optional products, and implicit products
				if doptional and self.exclusion_resolver.is_excluded(dprod.name, productName):
					continue;
				if dprod.name == "implicitProducts":
					continue;

				dependencies.append( self._add_product_tree(products, dprod.name) )

		# Construct EUPS version
		version = self.version_maker.version(productdir, ref, dependencies)

		# Add the result to products, return it for convenience
		products[productName] = Product(productName, sha1, version, dependencies)
		return products[productName]

	def prepare(self, productNames):
		products = dict()
		for name in productNames:
			self._add_product_tree(products, name)

		return Manifest.fromProductDict(products)

	@staticmethod
	def run(args):
		e = eups.Eups()

		# Ensure build directory exists and is writable
		build_dir = args.build_dir
		if not os.access(build_dir, os.W_OK):
			raise Exception("Directory '%s' does not exist or isn't writable." % build_dir)

		# Add 'master' to list of refs, if not there already
		refs = args.ref
		if 'master' not in refs:
			refs.append('master')

		# Wire-up the preparer
		if args.exclusion_map:
			with open(args.exclusion_map) as fp:
				exclusion_resolver = ExclusionResolver.fromFile(fp)
		else:
			exclusion_resolver = ExclusionResolver([])
		product_fetcher = ProductFetcher(build_dir, args.repository_pattern, refs, args.no_fetch)
		version_maker = VersionMaker(args.sha_abbrev_len)
		p = BuildDirectoryConstructor(build_dir, e, product_fetcher, version_maker, exclusion_resolver)

		# Run
		manifest = p.prepare(args.products)

		tags = eups.tags.Tags()
		tags.loadFromEupsPath(e.path)
		manifest.buildID = nextAvailableEupsBuildTag(tags)
		tags.registerTag(manifest.buildID)
		tags.saveGlobalTags(e.path[0])

		manifest.toFile(sys.stdout)
