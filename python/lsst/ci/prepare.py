#############################################################################
# Preparer

import os, os.path
import sys
import eups
import hashlib
import shutil

import tsort

from .git import Git, GitError

class Preparer(object):
	def __init__(self, build_dir, refs, repository_patterns, sha_abbrev_len, no_pull):
		self.build_dir = os.path.abspath(build_dir)
		self.refs = refs
		self.repository_patterns = repository_patterns.split('|')
		self.sha_abbrev_len = sha_abbrev_len
		self.no_pull = no_pull

		self.deps = []
		self.versions = {}

	def _origin_candidates(self, product):
		""" Expand repository_patterns into URLs. """
		data = { 'product': product }
		return [ pat % data for pat in self.repository_patterns ]

	def _prepare(self, product):
		print "Preparing ", product

		try:
			return self.versions[product]
		except KeyError:
			pass

		productdir = os.path.join(self.build_dir, product)

		if os.path.isdir(productdir):
			# Check that the remote hasn't changed; remove the clone if it did
			# so it will get cloned again
			git = Git(productdir)
			origin = git('config', '--get', 'remote.origin.url')
			for candidate in self._origin_candidates(product):
				if origin == candidate:
					break
			else:
				shutil.rmtree(productdir)

		# Clone the product, if needed
		if not os.path.isdir(productdir):
			if os.path.exists(productdir):
				raise Exception("%s exists and is not a directory. Cannot clone a git repository there." % productdir)

			for url in self._origin_candidates(product):
				try:
					Git.clone(url, productdir)
					break
				except GitError as e:
					print e
					print e.stderr
					print e.output
					pass
			else:
				raise Exception("Failed to clone product '%s' from any of the offered repositories" % product)

		git = Git(productdir)

		if not self.no_pull:
			# reset, clean, & pull
			git("fetch", "origin", "--prune")

		# Check out the first matching requested ref
		for ref in self.refs:
			oref = 'origin/%s' % ref
			try:
				sha1 = git('rev-parse', '--verify', '-q', oref)
			except GitError:
				sha1 = None

			if sha1:
				# avoid checking out if already checked out (speed)
				try:
					checkout = git('rev-parse', 'HEAD') != sha1
				except GitError:
					checkout = True

				if checkout:
					git('checkout', '-f', oref)
				break
		else:
			raise Exception("None of the specified refs exist in product '%s'" % product)

		# Clean up the working directory
		git("reset", "--hard")
		git("clean", "-d", "-f", "-q")

		# Parse the table file to discover dependencies
		dep_vers = []
		table_fn = os.path.join(productdir, 'ups', '%s.table' % product)
		if os.path.isfile(table_fn):
			# Choose which dependencies to prepare
			product_deps = []
			for dep in eups.table.Table(table_fn).dependencies(eups.Eups()):
				if dep[1] == True: continue				# skip optionals
				if dep[0].name == "implicitProducts": continue;		# skip implicit products
				product_deps.append(dep[0].name)

			# Recursively prepare the chosen dependencies
			for dep_product in product_deps:
				dep_ver = self._prepare(dep_product)[0]
				dep_vers.append(dep_ver)
				self.deps.append((dep_product, product))

		# Construct EUPS version
		ref = self._get_git_ref(git, product)
		version = self._construct_version(ref, dep_vers)

		# Store the result
		self.versions[product] = (version, ref)

		return self.versions[product]

	def _get_git_ref(self, git, product):
		""" Return a git ref to this product's source code. """
		return git('rev-parse', '--short=%d' % self.sha_abbrev_len, 'HEAD')

	def _construct_version(self, ref, dep_versions):
		""" Return a standardized XXX+YYY EUPS version, that includes the dependencies. """
		if dep_versions:
			deps_sha1 = self._depver_hash(dep_versions)
			return "%s+%s" % (ref, deps_sha1)
		else:
			return ref

	def _depver_hash(self, versions):
		""" Return a standardized hash of the list of versions """
		return hashlib.sha1('\n'.join(sorted(versions))).hexdigest()[:self.sha_abbrev_len]

	@staticmethod
	def run(args):
		# Ensure build directory exists and is writable
		build_dir = args.build_dir
		if not os.access(build_dir, os.W_OK):
			raise Exception("Directory '%s' does not exist or isn't writable." % build_dir)

		# Add 'master' to list of refs, if not there already
		refs = args.ref
		if 'master' not in refs:
			refs.append('master')
	
		# Prepare products
		p = Preparer(build_dir, refs, args.repository_pattern, args.sha_abbrev_len, args.no_pull)
		for product in args.products:
			p._prepare(product)

		# Topologically sort the result
		products = tsort.tsort(p.deps)
		print '# %-23s\t%s\t%s' % ("product", "git ref", "EUPS version")
		for product in products:
			print '%-25s\t%s\t%s' % (product, p.versions[product][1], p.versions[product][0])

