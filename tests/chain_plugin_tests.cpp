#include <boost/test/unit_test.hpp>
#include <boost/algorithm/string/predicate.hpp>

#include <eosio/testing/tester.hpp>
#include <eosio/chain/abi_serializer.hpp>
#include <eosio/chain/wasm_eosio_constraints.hpp>
#include <eosio/chain/resource_limits.hpp>
#include <eosio/chain/exceptions.hpp>
#include <eosio/chain/wast_to_wasm.hpp>
#include <eosio/chain_plugin/chain_plugin.hpp>

#include <contracts.hpp>

#include <fc/io/fstream.hpp>

#include <Runtime/Runtime.h>

#include <fc/variant_object.hpp>
#include <fc/io/json.hpp>

#include <array>
#include <utility>

#ifdef NON_VALIDATING_TEST
#define TESTER tester
#else
#define TESTER validating_tester
#endif

using namespace eosio;
using namespace eosio::chain;
using namespace eosio::testing;
using namespace appbase;
using namespace fc;

namespace std{
   std::ostream& operator << (std::ostream& s, fc::time_point tp){
      return s << (string)tp;
   }
}

std::string version_to_fixed_str(uint32_t ver){
   std::stringstream ss;
   ss << std::setfill('0') << std::setw(sizeof(uint32_t)<<1) << ver;
   return ss.str();
}



BOOST_AUTO_TEST_SUITE(chain_plugin_tests)

BOOST_FIXTURE_TEST_CASE( get_block_with_invalid_abi, TESTER ) try {
   produce_blocks(2);

   create_accounts( {"asserter"_n} );
   produce_block();

   // setup contract and abi
   set_code( "asserter"_n, contracts::asserter_wasm() );
   set_abi( "asserter"_n, contracts::asserter_abi().data() );
   produce_blocks(1);

   auto resolver = [&,this]( const account_name& name ) -> std::optional<abi_serializer> {
      try {
         const auto& accnt  = this->control->db().get<account_object,by_name>( name );
         abi_def abi;
         if (abi_serializer::to_abi(accnt.abi, abi)) {
            return abi_serializer(abi, abi_serializer::create_yield_function( abi_serializer_max_time ));
         }
         return std::optional<abi_serializer>();
      } FC_RETHROW_EXCEPTIONS(error, "resolver failed at chain_plugin_tests::abi_invalid_type");
   };

   // abi should be resolved
   BOOST_REQUIRE_EQUAL(true, resolver("asserter"_n).has_value());

   // make an action using the valid contract & abi
   fc::variant pretty_trx = mutable_variant_object()
      ("actions", variants({
         mutable_variant_object()
            ("account", "asserter")
            ("name", "procassert")
            ("authorization", variants({
               mutable_variant_object()
                  ("actor", "asserter")
                  ("permission", name(config::active_name).to_string())
            }))
            ("data", mutable_variant_object()
               ("condition", 1)
               ("message", "Should Not Assert!")
            )
         })
      );
   signed_transaction trx;
   abi_serializer::from_variant(pretty_trx, trx, resolver, abi_serializer::create_yield_function( abi_serializer_max_time ));
   set_transaction_headers(trx);
   trx.sign( get_private_key( "asserter"_n, "active" ), control->get_chain_id() );
   push_transaction( trx );
   produce_blocks(1);

   // retrieve block num
   uint32_t headnum = this->control->head_block_num();

   char headnumstr[20];
   sprintf(headnumstr, "%d", headnum);
   chain_apis::read_only::get_block_params param{headnumstr};
   chain_apis::read_only plugin(*(this->control), {}, fc::microseconds::maximum(), {});


   // block should be decoded successfully
   std::string block_str = json::to_pretty_string(plugin.get_block(param));
   BOOST_TEST(block_str.find("procassert") != std::string::npos);
   BOOST_TEST(block_str.find("condition") != std::string::npos);
   BOOST_TEST(block_str.find("Should Not Assert!") != std::string::npos);
   BOOST_TEST(block_str.find("011253686f756c64204e6f742041737365727421") != std::string::npos); //action data

   // set an invalid abi (int8->xxxx)
   std::string abi2 = contracts::asserter_abi().data();
   auto pos = abi2.find("int8");
   BOOST_TEST(pos != std::string::npos);
   abi2.replace(pos, 4, "xxxx");
   set_abi("asserter"_n, abi2.c_str());
   produce_blocks(1);

   // resolving the invalid abi result in exception
   BOOST_CHECK_THROW(resolver("asserter"_n), invalid_type_inside_abi);

   // get the same block as string, results in decode failed(invalid abi) but not exception
   std::string block_str2 = json::to_pretty_string(plugin.get_block(param));
   BOOST_TEST(block_str2.find("procassert") != std::string::npos);
   BOOST_TEST(block_str2.find("condition") == std::string::npos); // decode failed
   BOOST_TEST(block_str2.find("Should Not Assert!") == std::string::npos); // decode failed
   BOOST_TEST(block_str2.find("011253686f756c64204e6f742041737365727421") != std::string::npos); //action data

} FC_LOG_AND_RETHROW() /// get_block_with_invalid_abi

BOOST_FIXTURE_TEST_CASE( get_info, TESTER ) try {
   produce_blocks(1);

   chain_apis::read_only::get_info_params p;
   chain_apis::read_only plugin(*(this->control), {}, fc::microseconds::maximum(), {});

   auto info = plugin.get_info({});
   BOOST_TEST(info.server_version == version_to_fixed_str(app().version()));
   BOOST_TEST(info.chain_id == control->get_chain_id());
   BOOST_TEST(info.head_block_num == control->head_block_num());
   BOOST_TEST(info.last_irreversible_block_num == control->last_irreversible_block_num());
   BOOST_TEST(info.last_irreversible_block_id == control->last_irreversible_block_id());
   BOOST_TEST(info.head_block_id == control->head_block_id());
   BOOST_TEST(info.head_block_time == control->head_block_time());
   BOOST_TEST(info.head_block_producer == control->head_block_producer());
   BOOST_TEST(info.virtual_block_cpu_limit == control->get_resource_limits_manager().get_virtual_block_cpu_limit());
   BOOST_TEST(info.virtual_block_net_limit == control->get_resource_limits_manager().get_virtual_block_net_limit());
   BOOST_TEST(info.block_cpu_limit == control->get_resource_limits_manager().get_block_cpu_limit());
   BOOST_TEST(info.block_net_limit == control->get_resource_limits_manager().get_block_net_limit());
   BOOST_TEST(*info.server_version_string == app().version_string());
   BOOST_TEST(*info.fork_db_head_block_num == control->fork_db_pending_head_block_num());
   BOOST_TEST(*info.fork_db_head_block_id == control->fork_db_pending_head_block_id());
   BOOST_TEST(*info.server_full_version_string == app().full_version_string());
   BOOST_TEST(*info.last_irreversible_block_time == control->last_irreversible_block_time());
   BOOST_TEST(*info.total_cpu_weight == control->get_resource_limits_manager().get_total_cpu_weight());
   BOOST_TEST(*info.total_net_weight == control->get_resource_limits_manager().get_total_net_weight());

   produce_blocks(1);

   //make sure it works after producing new block
   info = plugin.get_info({});
   BOOST_TEST(info.server_version == version_to_fixed_str(app().version()));
   BOOST_TEST(info.chain_id == control->get_chain_id());
   BOOST_TEST(info.head_block_num == control->head_block_num());
   BOOST_TEST(info.last_irreversible_block_num == control->last_irreversible_block_num());
   BOOST_TEST(info.last_irreversible_block_id == control->last_irreversible_block_id());
   BOOST_TEST(info.head_block_id == control->head_block_id());
   BOOST_TEST(info.head_block_time == control->head_block_time());
   BOOST_TEST(info.head_block_producer == control->head_block_producer());
   BOOST_TEST(info.virtual_block_cpu_limit == control->get_resource_limits_manager().get_virtual_block_cpu_limit());
   BOOST_TEST(info.virtual_block_net_limit == control->get_resource_limits_manager().get_virtual_block_net_limit());
   BOOST_TEST(info.block_cpu_limit == control->get_resource_limits_manager().get_block_cpu_limit());
   BOOST_TEST(info.block_net_limit == control->get_resource_limits_manager().get_block_net_limit());
   BOOST_TEST(*info.server_version_string == app().version_string());
   BOOST_TEST(*info.fork_db_head_block_num == control->fork_db_pending_head_block_num());
   BOOST_TEST(*info.fork_db_head_block_id == control->fork_db_pending_head_block_id());
   BOOST_TEST(*info.server_full_version_string == app().full_version_string());
   BOOST_TEST(*info.last_irreversible_block_time == control->last_irreversible_block_time());
} FC_LOG_AND_RETHROW() //get_info

BOOST_FIXTURE_TEST_CASE( get_consensus_parameters, TESTER ) try {
   produce_blocks(1);

   chain_apis::read_only::get_info_params p;
   chain_apis::read_only plugin(*(this->control), {}, fc::microseconds::maximum(), {});

   auto parms = plugin.get_consensus_parameters({});

   // verifying chain_config
   BOOST_TEST(parms.chain_config.max_block_cpu_usage == control->get_global_properties().configuration.max_block_cpu_usage);
   BOOST_TEST(parms.chain_config.target_block_net_usage_pct == control->get_global_properties().configuration.target_block_net_usage_pct);
   BOOST_TEST(parms.chain_config.max_transaction_net_usage == control->get_global_properties().configuration.max_transaction_net_usage);
   BOOST_TEST(parms.chain_config.base_per_transaction_net_usage == control->get_global_properties().configuration.base_per_transaction_net_usage);
   BOOST_TEST(parms.chain_config.net_usage_leeway == control->get_global_properties().configuration.net_usage_leeway);
   BOOST_TEST(parms.chain_config.context_free_discount_net_usage_num == control->get_global_properties().configuration.context_free_discount_net_usage_num);
   BOOST_TEST(parms.chain_config.context_free_discount_net_usage_den == control->get_global_properties().configuration.context_free_discount_net_usage_den);
   BOOST_TEST(parms.chain_config.max_block_cpu_usage == control->get_global_properties().configuration.max_block_cpu_usage);
   BOOST_TEST(parms.chain_config.target_block_cpu_usage_pct == control->get_global_properties().configuration.target_block_cpu_usage_pct);
   BOOST_TEST(parms.chain_config.max_transaction_cpu_usage == control->get_global_properties().configuration.max_transaction_cpu_usage);
   BOOST_TEST(parms.chain_config.min_transaction_cpu_usage == control->get_global_properties().configuration.min_transaction_cpu_usage);
   BOOST_TEST(parms.chain_config.max_transaction_lifetime == control->get_global_properties().configuration.max_transaction_lifetime);
   BOOST_TEST(parms.chain_config.deferred_trx_expiration_window == control->get_global_properties().configuration.deferred_trx_expiration_window);
   BOOST_TEST(parms.chain_config.max_transaction_delay == control->get_global_properties().configuration.max_transaction_delay);
   BOOST_TEST(parms.chain_config.max_inline_action_size == control->get_global_properties().configuration.max_inline_action_size);
   BOOST_TEST(parms.chain_config.max_inline_action_depth == control->get_global_properties().configuration.max_inline_action_depth);
   BOOST_TEST(parms.chain_config.max_authority_depth == control->get_global_properties().configuration.max_authority_depth);
   BOOST_TEST(parms.chain_config.max_action_return_value_size == control->get_global_properties().configuration.max_action_return_value_size);

   // verifying kv_database_config
   BOOST_TEST(parms.kv_database_config.max_key_size == control->get_global_properties().kv_configuration.max_key_size);
   BOOST_TEST(parms.kv_database_config.max_value_size == control->get_global_properties().kv_configuration.max_value_size);
   BOOST_TEST(parms.kv_database_config.max_iterators == control->get_global_properties().kv_configuration.max_iterators);

   // verifying wasm_config
   BOOST_TEST(parms.wasm_config.max_mutable_global_bytes == control->get_global_properties().wasm_configuration.max_mutable_global_bytes);
   BOOST_TEST(parms.wasm_config.max_table_elements == control->get_global_properties().wasm_configuration.max_table_elements);
   BOOST_TEST(parms.wasm_config.max_section_elements == control->get_global_properties().wasm_configuration.max_section_elements);
   BOOST_TEST(parms.wasm_config.max_linear_memory_init == control->get_global_properties().wasm_configuration.max_linear_memory_init);
   BOOST_TEST(parms.wasm_config.max_func_local_bytes == control->get_global_properties().wasm_configuration.max_func_local_bytes);
   BOOST_TEST(parms.wasm_config.max_nested_structures == control->get_global_properties().wasm_configuration.max_nested_structures);
   BOOST_TEST(parms.wasm_config.max_symbol_bytes == control->get_global_properties().wasm_configuration.max_symbol_bytes);
   BOOST_TEST(parms.wasm_config.max_module_bytes == control->get_global_properties().wasm_configuration.max_module_bytes);
   BOOST_TEST(parms.wasm_config.max_code_bytes == control->get_global_properties().wasm_configuration.max_code_bytes);
   BOOST_TEST(parms.wasm_config.max_pages == control->get_global_properties().wasm_configuration.max_pages);
   BOOST_TEST(parms.wasm_config.max_call_depth == control->get_global_properties().wasm_configuration.max_call_depth);

} FC_LOG_AND_RETHROW() //get_consensus_parameters

BOOST_FIXTURE_TEST_CASE( get_all_accounts, TESTER ) try {
   produce_blocks(2);

   std::vector<account_name> accs{{ "alice"_n, "bob"_n, "cindy"_n}};
   create_accounts(accs);

   produce_block();

   chain_apis::read_only plugin(*(this->control), {}, fc::microseconds::maximum(), {});

   chain_apis::read_only::get_all_accounts_params p;
   p.limit = 6;
   chain_apis::read_only::get_all_accounts_result result = plugin.read_only::get_all_accounts(p);

   //BOOST_REQUIRE(!result.more.has_value());
   BOOST_REQUIRE_EQUAL(6u, result.accounts.size());
   if (result.accounts.size() >= 6) {
      BOOST_REQUIRE_EQUAL(name("alice"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("bob"_n), result.accounts[1].name);
      BOOST_REQUIRE_EQUAL(name("cindy"_n), result.accounts[2].name);
      BOOST_REQUIRE_EQUAL(name("eosio"_n), result.accounts[3].name);
      BOOST_REQUIRE_EQUAL(name("eosio.null"_n), result.accounts[4].name);
      BOOST_REQUIRE_EQUAL(name("eosio.prods"_n), result.accounts[5].name);
   }

   // limit bigger than result
   p.limit = 12;
   result = plugin.read_only::get_all_accounts(p);

   BOOST_REQUIRE(!result.more.has_value());
   BOOST_REQUIRE_EQUAL(6u, result.accounts.size());
   if (result.accounts.size() >= 6) {
      BOOST_REQUIRE_EQUAL(name("alice"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("bob"_n), result.accounts[1].name);
      BOOST_REQUIRE_EQUAL(name("cindy"_n), result.accounts[2].name);
      BOOST_REQUIRE_EQUAL(name("eosio"_n), result.accounts[3].name);
      BOOST_REQUIRE_EQUAL(name("eosio.null"_n), result.accounts[4].name);
      BOOST_REQUIRE_EQUAL(name("eosio.prods"_n), result.accounts[5].name);
   }

   // reverse
   p.reverse = true;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE_EQUAL(6u, result.accounts.size());
   if (result.accounts.size() >= 6) {
      BOOST_REQUIRE_EQUAL(name("eosio.prods"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("eosio.null"_n), result.accounts[1].name);
      BOOST_REQUIRE_EQUAL(name("eosio"_n), result.accounts[2].name);
      BOOST_REQUIRE_EQUAL(name("cindy"_n), result.accounts[3].name);
      BOOST_REQUIRE_EQUAL(name("bob"_n), result.accounts[4].name);
      BOOST_REQUIRE_EQUAL(name("alice"_n), result.accounts[5].name);
   }

   // reverse limit bigger than result
   p.limit = 12;
   result = plugin.read_only::get_all_accounts(p);

   BOOST_REQUIRE(!result.more.has_value());
   BOOST_REQUIRE_EQUAL(6u, result.accounts.size());
   if (result.accounts.size() >= 6) {
      BOOST_REQUIRE_EQUAL(name("eosio.prods"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("eosio.null"_n), result.accounts[1].name);
      BOOST_REQUIRE_EQUAL(name("eosio"_n), result.accounts[2].name);
      BOOST_REQUIRE_EQUAL(name("cindy"_n), result.accounts[3].name);
      BOOST_REQUIRE_EQUAL(name("bob"_n), result.accounts[4].name);
      BOOST_REQUIRE_EQUAL(name("alice"_n), result.accounts[5].name);
   }

   // pagination
   p.limit = 2;
   p.reverse = false;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(result.more.has_value());
   if (result.more.has_value())
      BOOST_REQUIRE_EQUAL("cindy"_n, *result.more);
   BOOST_REQUIRE_EQUAL(2u, result.accounts.size());
   if (result.accounts.size() >= 2) {
      BOOST_REQUIRE_EQUAL(name("alice"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("bob"_n), result.accounts[1].name);
   }

   p.lower_bound = *result.more;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(result.more.has_value());
   if (result.more.has_value())
      BOOST_REQUIRE_EQUAL("eosio.null"_n, *result.more);
   BOOST_REQUIRE_EQUAL(2u, result.accounts.size());
   if (result.accounts.size() >= 2) {
      BOOST_REQUIRE_EQUAL(name("cindy"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("eosio"_n), result.accounts[1].name);
   }

   p.lower_bound = *result.more;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(!result.more.has_value());
   BOOST_REQUIRE_EQUAL(2u, result.accounts.size());
   if (result.accounts.size() >= 2) {
      BOOST_REQUIRE_EQUAL(name("eosio.null"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("eosio.prods"_n), result.accounts[1].name);
   }

   // reverse pagination
   p.reverse = true;
   p.lower_bound.reset();
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(result.more.has_value());
   if (result.more.has_value())
      BOOST_REQUIRE_EQUAL("eosio"_n, *result.more);
   BOOST_REQUIRE_EQUAL(2u, result.accounts.size());
   if (result.accounts.size() >= 2) {
      BOOST_REQUIRE_EQUAL(name("eosio.prods"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("eosio.null"_n), result.accounts[1].name);
   }

   p.upper_bound = *result.more;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(result.more.has_value());
   if (result.more.has_value())
      BOOST_REQUIRE_EQUAL("bob"_n, *result.more);
   BOOST_REQUIRE_EQUAL(2u, result.accounts.size());
   if (result.accounts.size() >= 2) {
      BOOST_REQUIRE_EQUAL(name("eosio"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("cindy"_n), result.accounts[1].name);
   }

   p.upper_bound = *result.more;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(!result.more.has_value());
   BOOST_REQUIRE_EQUAL(2u, result.accounts.size());
   if (result.accounts.size() >= 2) {
      BOOST_REQUIRE_EQUAL(name("bob"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("alice"_n), result.accounts[1].name);
   }

   // pagination with prime # of accounts
   accs.clear();
   accs.push_back("gwen"_n);
   create_accounts(accs);

   produce_block();

   p.reverse = false;
   p.lower_bound.reset();
   p.upper_bound.reset();
   p.limit = 3;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(result.more.has_value());
   if (result.more.has_value())
      BOOST_REQUIRE_EQUAL("eosio"_n, *result.more);
   BOOST_REQUIRE_EQUAL(3u, result.accounts.size());
   if (result.accounts.size() >= 3) {
      BOOST_REQUIRE_EQUAL(name("alice"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("bob"_n), result.accounts[1].name);
      BOOST_REQUIRE_EQUAL(name("cindy"_n), result.accounts[2].name);
   }

   p.lower_bound = *result.more;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(result.more.has_value());
   if (result.more.has_value())
      BOOST_REQUIRE_EQUAL("gwen"_n, *result.more);
   BOOST_REQUIRE_EQUAL(3u, result.accounts.size());
   if (result.accounts.size() >= 3) {
      BOOST_REQUIRE_EQUAL(name("eosio"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("eosio.null"_n), result.accounts[1].name);
      BOOST_REQUIRE_EQUAL(name("eosio.prods"_n), result.accounts[2].name);
   }

   p.lower_bound = *result.more;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(!result.more.has_value());
   BOOST_REQUIRE_EQUAL(1u, result.accounts.size());
   if (result.accounts.size() >= 1) {
      BOOST_REQUIRE_EQUAL(name("gwen"_n), result.accounts[0].name);
   }

   // reverse pagination, prime # of accounts
   p.reverse = true;
   p.lower_bound.reset();
   p.upper_bound.reset();
   p.limit = 3;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(result.more.has_value());
   if (result.more.has_value())
      BOOST_REQUIRE_EQUAL("eosio"_n, *result.more);
   BOOST_REQUIRE_EQUAL(3u, result.accounts.size());
   if (result.accounts.size() >= 3) {
      BOOST_REQUIRE_EQUAL(name("gwen"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("eosio.prods"_n), result.accounts[1].name);
      BOOST_REQUIRE_EQUAL(name("eosio.null"_n), result.accounts[2].name);
   }

   p.upper_bound = *result.more;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(result.more.has_value());
   if (result.more.has_value())
      BOOST_REQUIRE_EQUAL("alice"_n, *result.more);
   BOOST_REQUIRE_EQUAL(3u, result.accounts.size());
   if (result.accounts.size() >= 3) {
      BOOST_REQUIRE_EQUAL(name("eosio"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("cindy"_n), result.accounts[1].name);
      BOOST_REQUIRE_EQUAL(name("bob"_n), result.accounts[2].name);
   }

   p.upper_bound = *result.more;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(!result.more.has_value());
   BOOST_REQUIRE_EQUAL(1u, result.accounts.size());
   if (result.accounts.size() >= 1) {
      BOOST_REQUIRE_EQUAL(name("alice"_n), result.accounts[0].name);
   }

   // lower and upper bound
   p.limit = 10;
   p.lower_bound = "b"_n;
   p.upper_bound = "g"_n;
   p.reverse = false;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(!result.more.has_value());
   BOOST_REQUIRE_EQUAL(5u, result.accounts.size());
   if (result.accounts.size() >= 1) {
      BOOST_REQUIRE_EQUAL(name("bob"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("cindy"_n), result.accounts[1].name);
      BOOST_REQUIRE_EQUAL(name("eosio"_n), result.accounts[2].name);
      BOOST_REQUIRE_EQUAL(name("eosio.null"_n), result.accounts[3].name);
      BOOST_REQUIRE_EQUAL(name("eosio.prods"_n), result.accounts[4].name);
   }

   // lower and upper bound, reverse
   p.reverse = true;
   result = plugin.read_only::get_all_accounts(p);
   BOOST_REQUIRE(!result.more.has_value());
   BOOST_REQUIRE_EQUAL(5u, result.accounts.size());
   if (result.accounts.size() >= 1) {
      BOOST_REQUIRE_EQUAL(name("eosio.prods"_n), result.accounts[0].name);
      BOOST_REQUIRE_EQUAL(name("eosio.null"_n), result.accounts[1].name);
      BOOST_REQUIRE_EQUAL(name("eosio"_n), result.accounts[2].name);
      BOOST_REQUIRE_EQUAL(name("cindy"_n), result.accounts[3].name);
      BOOST_REQUIRE_EQUAL(name("bob"_n), result.accounts[4].name);
   }

} FC_LOG_AND_RETHROW() //get_all_accounts

BOOST_FIXTURE_TEST_CASE( get_account, TESTER ) try {
   produce_blocks(2);

   std::vector<account_name> accs{{ "alice"_n, "bob"_n, "cindy"_n}};
   create_accounts(accs, false, false);

   produce_block();

   chain_apis::read_only plugin(*(this->control), {}, fc::microseconds::maximum(), {});

   chain_apis::read_only::get_account_params p{"alice"_n};

   chain_apis::read_only::get_account_results result = plugin.read_only::get_account(p);

   auto check_result_basic = [](chain_apis::read_only::get_account_results result, chain::name nm, bool isPriv) {
      BOOST_REQUIRE_EQUAL(nm, result.account_name);
      BOOST_REQUIRE_EQUAL(isPriv, result.privileged);

      BOOST_REQUIRE_EQUAL(2, result.permissions.size());
      if (result.permissions.size() > 1) {
         auto perm = result.permissions[0];
         BOOST_REQUIRE_EQUAL(name("active"_n), perm.perm_name); 
         BOOST_REQUIRE_EQUAL(name("owner"_n), perm.parent);
         auto auth = perm.required_auth;
         BOOST_REQUIRE_EQUAL(1, auth.threshold);
         BOOST_REQUIRE_EQUAL(1, auth.keys.size());
         BOOST_REQUIRE_EQUAL(0, auth.accounts.size());
         BOOST_REQUIRE_EQUAL(0, auth.waits.size());

         perm = result.permissions[1];
         BOOST_REQUIRE_EQUAL(name("owner"_n), perm.perm_name); 
         BOOST_REQUIRE_EQUAL(name(""_n), perm.parent); 
         auth = perm.required_auth;
         BOOST_REQUIRE_EQUAL(1, auth.threshold);
         BOOST_REQUIRE_EQUAL(1, auth.keys.size());
         BOOST_REQUIRE_EQUAL(0, auth.accounts.size());
         BOOST_REQUIRE_EQUAL(0, auth.waits.size());
      }
   };

   check_result_basic(result, name("alice"_n), false);

   for (auto perm : result.permissions) {
      BOOST_REQUIRE_EQUAL(true, perm.linked_actions.has_value());
      if (perm.linked_actions.has_value())
         BOOST_REQUIRE_EQUAL(0, perm.linked_actions->size());
   }
   BOOST_REQUIRE_EQUAL(0, result.eosio_any_linked_actions.size());

   // test link authority
   link_authority(name("alice"_n), name("bob"_n), name("active"_n), name("foo"_n));
   produce_block();
   result = plugin.read_only::get_account(p);

   check_result_basic(result, name("alice"_n), false);
   auto perm = result.permissions[0];
   BOOST_REQUIRE_EQUAL(1, perm.linked_actions->size());
   if (perm.linked_actions->size() >= 1) {
      auto la = (*perm.linked_actions)[0];
      BOOST_REQUIRE_EQUAL(name("bob"_n), la.account);
      BOOST_REQUIRE_EQUAL(true, la.action.has_value());
      if(la.action.has_value()) {
         BOOST_REQUIRE_EQUAL(name("foo"_n), la.action.value());
      }
   }
   BOOST_REQUIRE_EQUAL(0, result.eosio_any_linked_actions.size());

   // test link authority to eosio.any
   link_authority(name("alice"_n), name("bob"_n), name("eosio.any"_n), name("foo"_n));
   produce_block();
   result = plugin.read_only::get_account(p);
   check_result_basic(result, name("alice"_n), false);
   // active permission should no longer have linked auth, as eosio.any replaces it
   perm = result.permissions[0];
   BOOST_REQUIRE_EQUAL(0, perm.linked_actions->size());

   auto eosio_any_la = result.eosio_any_linked_actions;
   BOOST_REQUIRE_EQUAL(1, eosio_any_la.size());
   if (eosio_any_la.size() >= 1) {
      auto la = eosio_any_la[0];
      BOOST_REQUIRE_EQUAL(name("bob"_n), la.account);
      BOOST_REQUIRE_EQUAL(true, la.action.has_value());
      if(la.action.has_value()) {
         BOOST_REQUIRE_EQUAL(name("foo"_n), la.action.value());
      }
   }
} FC_LOG_AND_RETHROW() /// get_account

BOOST_FIXTURE_TEST_CASE( get_genesis, TESTER ) try {
   produce_blocks(2);

   chain::genesis_state default_genesis;

   chain_apis::read_only plugin(*(this->control), {}, fc::microseconds::maximum(), default_genesis);

   chain_apis::read_only::get_genesis_result result = plugin.read_only::get_genesis({});

   BOOST_REQUIRE_EQUAL(result.initial_configuration, default_genesis.initial_configuration);   
} FC_LOG_AND_RETHROW() /// get_genesis

BOOST_AUTO_TEST_SUITE_END()
